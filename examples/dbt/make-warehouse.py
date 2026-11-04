"""Build the Iceberg warehouse the dbt example reads: customers, weekly order loads, churn labels.

    python make-warehouse.py ./warehouse

The catalog is a PyIceberg SQL catalog named `jaffle` (SQLite metadata + local files), so dbt's
`iceberg` plugin and `sqlguard --catalog jaffle` open the same tables. Orders arrive in weekly
commits; each commit records the time it became readable in `sqlguard.logical-ts`, which is what
point-in-time replay pins to. Everything is generated from a fixed seed.
"""

from __future__ import annotations

import random
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pyarrow as pa
from pyiceberg.catalog.sql import SqlCatalog

CATALOG = "jaffle"
NAMESPACE = "raw"
LOGICAL_TS = "sqlguard.logical-ts"
START = datetime(2024, 1, 1)
END = datetime(2024, 8, 1)  # orders are loaded up to here
CUTOFFS = [datetime(2024, m, 1) for m in (3, 4, 5, 6)]
HORIZON = timedelta(days=30)  # a customer churns when they order nothing in the 30 days after
N_CUSTOMERS = 40

ts = pa.timestamp("us")
SCHEMAS = {
    "customers": pa.schema(
        [("id", pa.int64()), ("first_name", pa.string()), ("last_name", pa.string())]
    ),
    "orders": pa.schema(
        [
            ("id", pa.int64()),
            ("user_id", pa.int64()),
            ("ordered_at", ts),
            ("loaded_at", ts),
            ("status", pa.string()),
            ("amount", pa.float64()),
        ]
    ),
    "churn_labels": pa.schema(
        [("customer_id", pa.int64()), ("cutoff_ts", ts), ("churned", pa.bool_())]
    ),
}
FIRST = ["Michael", "Shawn", "Kathleen", "Jimmy", "Katherine", "Sarah", "Martin", "Frank"]
LAST = ["P.", "M.", "P.", "C.", "R.", "R.", "M.", "R."]


def build(warehouse: Path) -> None:
    warehouse = warehouse.resolve()
    warehouse.mkdir(parents=True, exist_ok=True)
    catalog = SqlCatalog(
        CATALOG, uri=f"sqlite:///{warehouse / 'catalog.db'}", warehouse=f"file://{warehouse}"
    )
    catalog.create_namespace_if_not_exists(NAMESPACE)
    tables = {
        name: catalog.create_table(f"{NAMESPACE}.{name}", schema=schema)
        for name, schema in SCHEMAS.items()
    }

    rng = random.Random(11)
    customers = [
        {"id": i, "first_name": FIRST[i % len(FIRST)], "last_name": LAST[i % len(LAST)]}
        for i in range(1, N_CUSTOMERS + 1)
    ]
    tables["customers"].append(pa.Table.from_pylist(customers, schema=SCHEMAS["customers"]))

    # Each customer orders at their own pace and stops at a random day; the pace fades before.
    pace = {c["id"]: rng.uniform(0.05, 0.5) for c in customers}  # orders per day
    stops = {
        c["id"]: START + timedelta(days=rng.randint(70, 200))
        for c in customers
        if rng.random() < 0.5
    }
    orders: list[dict] = []
    order_id = 0
    day = START
    while day < END:
        for cid, rate in pace.items():
            stop = stops.get(cid)
            if stop is not None and day >= stop:
                continue
            fade = 1.0 if stop is None else min(1.0, (stop - day).days / 30)
            if rng.random() < rate * fade:
                order_id += 1
                at = day + timedelta(seconds=rng.randint(0, 86_399))
                orders.append(
                    {
                        "id": order_id,
                        "user_id": cid,
                        "ordered_at": at,
                        "status": rng.choice(["placed", "shipped", "completed"]),
                        "amount": round(rng.uniform(5, 80), 2),
                    }
                )
        day += timedelta(days=1)

    # one commit per week; a row is loaded when its commit lands (the Monday after it was placed)
    week = START
    while week < END:
        landed = week + timedelta(days=7)
        batch = [{**o, "loaded_at": landed} for o in orders if week <= o["ordered_at"] < landed]
        if batch:
            tables["orders"].append(
                pa.Table.from_pylist(batch, schema=SCHEMAS["orders"]),
                snapshot_properties={LOGICAL_TS: landed.isoformat()},
            )
        week = landed

    labels = []
    for cutoff in CUTOFFS:
        for cid in pace:
            if cid in stops and stops[cid] <= cutoff:
                continue  # already gone: nobody predicts for them
            later = [
                o
                for o in orders
                if o["user_id"] == cid and cutoff <= o["ordered_at"] < cutoff + HORIZON
            ]
            labels.append({"customer_id": cid, "cutoff_ts": cutoff, "churned": not later})
    tables["churn_labels"].append(pa.Table.from_pylist(labels, schema=SCHEMAS["churn_labels"]))
    print(
        f"{warehouse}: {len(customers)} customers, {len(orders)} orders in "
        f"{len(tables['orders'].snapshots())} commits, {len(labels)} labels"
    )


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    build(Path(sys.argv[1]))
