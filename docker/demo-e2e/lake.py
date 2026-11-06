"""Lakehouse side of the end-to-end demo, straight on the catalog and never through the guard.

    lake.py seed       create prod.orders: 100 rows in four commits, 30 of them cancelled
    lake.py state      rows, the snapshot main is on, leftover branches
    lake.py foreign    another writer appends 10 rows to prod.orders (it has plain catalog rights)
    lake.py history T  the first and last snapshots of table T with commit time and logical time

`state` is the ground truth the demo compares the agent's and the reviewer's accounts with: it
reads the table with PyIceberg, not through the MCP server. The catalog comes from the
PYICEBERG_CATALOG__LAKEHOUSE__* variables, like everything else in the demo. Demo data only.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime

import pyarrow as pa
from pyiceberg.catalog import load_catalog

LOGICAL_TS = "sqlguard.logical-ts"
CATALOG = "lakehouse"
NAMESPACE = "prod"
TABLE = f"{NAMESPACE}.orders"
SCHEMA = pa.schema(
    [
        ("id", pa.int64()),
        ("customer_id", pa.int32()),
        ("status", pa.string()),
        ("amount", pa.float64()),
    ]
)


def order_rows(first: int, last: int) -> list[dict]:
    """Orders `first..last`; those with id % 10 < 3 are cancelled (30 of the first 100)."""
    return [
        {
            "id": i,
            "customer_id": i % 10 + 1,
            "status": "cancelled" if i % 10 < 3 else "paid",
            "amount": i * 1.5,
        }
        for i in range(first, last + 1)
    ]


def seed() -> None:
    catalog = load_catalog(CATALOG)
    catalog.create_namespace_if_not_exists(NAMESPACE)
    table = catalog.create_table(TABLE, schema=SCHEMA)
    for first in (1, 26, 51, 76):
        table.append(pa.Table.from_pylist(order_rows(first, first + 24), schema=SCHEMA))
    state()


def state() -> None:
    table = load_catalog(CATALOG).load_table(TABLE)
    rows = table.scan().to_arrow()
    current = table.current_snapshot()
    cancelled = sum(1 for s in rows.column("status").to_pylist() if s == "cancelled")
    print(
        f"catalog> {TABLE}: {rows.num_rows} rows ({cancelled} cancelled); "
        f"main is on snapshot {current.snapshot_id if current else None}"
    )
    branches = sorted(name for name in table.refs() if name != "main")
    print(f"catalog> branches besides main: {', '.join(branches) if branches else 'none'}")


def foreign() -> None:
    """A job with plain catalog rights appends to prod.orders, straight to main."""
    table = load_catalog(CATALOG).load_table(TABLE)
    table.append(pa.Table.from_pylist(order_rows(101, 110), schema=SCHEMA))
    current = table.current_snapshot()
    print(
        f"other writer> appended 10 rows (ids 101-110) to {TABLE}; "
        f"main moved to snapshot {current.snapshot_id if current else None}"
    )


def history(name: str, edge: int = 2) -> None:
    """Commit time against `sqlguard.logical-ts` for the first and last snapshots of a table."""
    table = load_catalog(CATALOG).load_table(name)
    snaps = sorted(table.snapshots(), key=lambda s: s.sequence_number or 0)  # commit order
    print(f"catalog> {name}: {len(snaps)} snapshots; commit time -> logical time recorded on it")
    shown = snaps if len(snaps) <= 2 * edge else [*snaps[:edge], None, *snaps[-edge:]]
    for snap in shown:
        if snap is None:
            print(f"           ... {len(snaps) - 2 * edge} more")
            continue
        committed = datetime.fromtimestamp(snap.timestamp_ms / 1000, tz=UTC)
        logical = (snap.summary.additional_properties if snap.summary else {}).get(LOGICAL_TS)
        print(f"           {snap.snapshot_id}  {committed:%H:%M:%S.%f}"[:-3] + f"  ->  {logical}")


COMMANDS = {"seed": seed, "state": state, "foreign": foreign}

if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "history":
        history(sys.argv[2])
    elif len(sys.argv) == 2 and sys.argv[1] in COMMANDS:
        COMMANDS[sys.argv[1]]()
    else:
        raise SystemExit(__doc__)
