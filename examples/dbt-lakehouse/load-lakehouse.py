"""Load the shop's raw tables into an Iceberg catalog as a night-by-night history.

    python load-lakehouse.py --catalog lakehouse          # PYICEBERG_CATALOG__LAKEHOUSE__* settings

The catalog is any PyIceberg catalog (the end-to-end demo uses Lakekeeper). It creates the
namespace `raw` with `customers`, `churn_labels` and `orders`. `orders` gets one Iceberg commit
per night, the way a nightly job would write it: the job of night N appends the orders placed on
day N-1 plus late rows, orders placed one to three days earlier that reached the warehouse late
(about one in five). Every row carries the time its commit stands for in `loaded_at`.

The commits are written within seconds of each other, so their commit times say nothing about the
simulated nights. Each commit therefore records the night it stands for in the snapshot property
`sqlguard.logical-ts`, which point-in-time replay uses instead of the commit time. A real nightly
job would not need it: its commit times are the nights.

Everything is generated from a fixed seed.
"""

from __future__ import annotations

import argparse
import random
from datetime import datetime, timedelta

import pyarrow as pa
from pyiceberg.catalog import Catalog, load_catalog

NAMESPACE = "raw"
LOGICAL_TS = "sqlguard.logical-ts"
START = datetime(2026, 9, 1)
DAYS = 28  # orders are placed on days 0..27
CUTOFF_DAYS = (10, 14, 18, 21)  # prediction dates, as days after START
HORIZON = timedelta(days=7)  # a customer churns when they order nothing in the 7 days after
NIGHTLY_AT = timedelta(hours=2)  # the nightly job runs at 02:00
LATE_SHARE = 0.2
MAX_DELAY_DAYS = 3
N_CUSTOMERS = 40
SEED = 7

ts = pa.timestamp("us")
SCHEMAS = {
    "customers": pa.schema([("id", pa.int64()), ("first_name", pa.string())]),
    "orders": pa.schema(
        [
            ("id", pa.int64()),
            ("user_id", pa.int64()),
            ("ordered_at", ts),
            ("loaded_at", ts),
            ("amount", pa.float64()),
        ]
    ),
    "churn_labels": pa.schema(
        [("customer_id", pa.int64()), ("cutoff_ts", ts), ("churned", pa.bool_())]
    ),
}
FIRST_NAMES = ["Ada", "Grace", "Linus", "Edsger", "Barbara", "Ken", "Margaret", "Dennis"]


def simulate(seed: int = SEED) -> tuple[list[dict], list[dict], list[dict]]:
    """Customers, orders (with the night each one lands) and churn labels. Pure and seeded."""
    rng = random.Random(seed)
    customers = [{"id": i, "first_name": FIRST_NAMES[i % 8]} for i in range(1, N_CUSTOMERS + 1)]
    pace = {c["id"]: rng.uniform(0.1, 0.6) for c in customers}  # orders per day
    stops = {
        c["id"]: START + timedelta(days=rng.randint(12, 26))
        for c in customers
        if rng.random() < 0.5
    }
    orders: list[dict] = []
    for day in range(DAYS):
        for cid, rate in pace.items():
            placed_on = START + timedelta(days=day)
            stop = stops.get(cid)
            if stop is not None and placed_on >= stop:
                continue
            if rng.random() >= rate:
                continue
            delay = rng.randint(1, MAX_DELAY_DAYS) if rng.random() < LATE_SHARE else 0
            landed = START + timedelta(days=day + 1 + delay) + NIGHTLY_AT
            orders.append(
                {
                    "id": len(orders) + 1,
                    "user_id": cid,
                    "ordered_at": placed_on + timedelta(seconds=rng.randint(0, 86_399)),
                    "loaded_at": landed,
                    "amount": round(rng.uniform(5, 80), 2),
                }
            )
    labels = []
    for offset in CUTOFF_DAYS:
        cutoff = START + timedelta(days=offset)
        for cid in pace:
            if cid in stops and stops[cid] <= cutoff:
                continue  # already gone: nobody predicts for them
            later = [
                o
                for o in orders
                if o["user_id"] == cid and cutoff <= o["ordered_at"] < cutoff + HORIZON
            ]
            labels.append({"customer_id": cid, "cutoff_ts": cutoff, "churned": not later})
    return customers, orders, labels


def nightly_batches(orders: list[dict]) -> list[tuple[datetime, list[dict]]]:
    """The orders grouped by the night they land, in order."""
    nights = sorted({o["loaded_at"] for o in orders})
    return [(night, [o for o in orders if o["loaded_at"] == night]) for night in nights]


def late_rows(orders: list[dict]) -> int:
    """Orders that landed after the night that follows their own day."""
    return sum(
        1
        for o in orders
        if o["loaded_at"]
        > datetime.combine(o["ordered_at"].date(), datetime.min.time())
        + timedelta(days=1)
        + NIGHTLY_AT
    )


def load(catalog: Catalog) -> dict[str, int]:
    """Create the `raw` tables and write them; returns what was written."""
    customers, orders, labels = simulate()
    catalog.create_namespace_if_not_exists(NAMESPACE)
    tables = {
        name: catalog.create_table(f"{NAMESPACE}.{name}", schema=schema)
        for name, schema in SCHEMAS.items()
    }
    tables["customers"].append(
        pa.Table.from_pylist(customers, schema=SCHEMAS["customers"]),
        snapshot_properties={LOGICAL_TS: START.isoformat()},
    )
    batches = nightly_batches(orders)
    for night, rows in batches:
        tables["orders"].append(
            pa.Table.from_pylist(rows, schema=SCHEMAS["orders"]),
            snapshot_properties={LOGICAL_TS: night.isoformat()},
        )
    tables["churn_labels"].append(
        pa.Table.from_pylist(labels, schema=SCHEMAS["churn_labels"]),
        snapshot_properties={LOGICAL_TS: START.isoformat()},
    )
    return {
        "customers": len(customers),
        "orders": len(orders),
        "commits": len(batches),
        "late_rows": late_rows(orders),
        "labels": len(labels),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--catalog", required=True, help="PyIceberg catalog name")
    args = parser.parse_args()
    done = load(load_catalog(args.catalog))
    print(
        f"{NAMESPACE}.orders: {done['orders']} orders in {done['commits']} nightly commits "
        f"({done['late_rows']} late rows); {done['customers']} customers, {done['labels']} labels"
    )
    print(
        f"each commit records the night it stands for in the snapshot property {LOGICAL_TS}: "
        "replay pins to it, because the commits were written seconds apart"
    )


if __name__ == "__main__":
    main()
