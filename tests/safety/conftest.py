"""Fixtures for the execution tests: a throwaway warehouse and a Guard wired to it."""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime
from pathlib import Path

import pyarrow as pa
import pytest

from sqlguard.core.catalog import LakehouseCatalog
from sqlguard.safety import Policy, TaskScope
from sqlguard.safety.audit import AuditLog
from sqlguard.safety.guard import Guard

ROWS = 20


@pytest.fixture
def scope() -> TaskScope:
    return TaskScope.of(namespaces=["db"])


@pytest.fixture
def orders(catalog: LakehouseCatalog) -> LakehouseCatalog:
    """`db.orders` with 20 rows (ids 1..20) in a single snapshot."""
    catalog.create_table(
        "orders",
        pa.table(
            {
                "id": pa.array(range(1, ROWS + 1), pa.int64()),
                "customer": pa.array([f"c{i % 4}" for i in range(ROWS)]),
                "amount": pa.array([float(i) for i in range(ROWS)]),
            }
        ),
    )
    return catalog


@pytest.fixture
def audit_path(tmp_path: Path) -> Path:
    return tmp_path / "audit" / "audit.jsonl"


@pytest.fixture
def make_guard(orders: LakehouseCatalog, audit_path: Path):
    def build(policy: Policy | None = None, **kwargs) -> Guard:
        return Guard(orders, policy, audit_path=audit_path, **kwargs)

    return build


@pytest.fixture
def guard(make_guard) -> Guard:
    return make_guard()


@pytest.fixture
def audit(audit_path: Path) -> AuditLog:
    return AuditLog(audit_path)


def main_ids(catalog: LakehouseCatalog, table: str = "orders") -> list[int]:
    """Snapshot ids on main's lineage, oldest first."""
    iceberg = catalog.table(table)
    chain = []
    current = iceberg.current_snapshot()
    while current is not None:
        chain.append(current.snapshot_id)
        parent = current.parent_snapshot_id
        current = iceberg.snapshot_by_id(parent) if parent is not None else None
    return chain[::-1]


def order_ids(catalog: LakehouseCatalog, table: str = "orders") -> list[int]:
    return sorted(catalog.read(table).column("id").to_pylist())


def utc(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, tzinfo=UTC)


CITIES = ["Hà Nội", "a/b", "plain", "x:y", "100%", "a+b", "k=v&w"]


@pytest.fixture
def cities(catalog: LakehouseCatalog) -> LakehouseCatalog:
    """`db.cities`, identity-partitioned by `city` with values that need percent-encoding."""
    data = pa.table(
        {"id": pa.array(range(len(CITIES)), pa.int64()), "city": pa.array(CITIES)},
    )
    table = catalog.catalog.create_table(catalog.identifier("cities"), schema=data.schema)
    with table.update_spec() as spec:
        spec.add_identity("city")
    catalog.table("cities").append(data)
    return catalog


def age_files(root: Path, days: float = 60) -> None:
    """Make every file under `root` look `days` old."""
    old = time.time() - days * 86400
    for path in root.rglob("*"):
        if path.is_file():
            os.utime(path, (old, old))


@pytest.fixture
def race_on_next_commit(monkeypatch, orders: LakehouseCatalog):
    """Arm with `arm()`: the next snapshot-ref commit is preceded by a real concurrent append.

    The append goes through a separate table handle, so the commit that follows fails
    PyIceberg's main-ref assertion exactly as it would against a second process.
    """
    from pyiceberg.table.update.snapshot import ManageSnapshots

    state = {"armed": False}
    original = ManageSnapshots.commit

    def racing_commit(self):
        if state["armed"]:
            state["armed"] = False
            orders.table("orders").append(
                pa.table(
                    {
                        "id": pa.array([999], pa.int64()),
                        "customer": ["racer"],
                        "amount": pa.array([1.0]),
                    }
                )
            )
        return original(self)

    monkeypatch.setattr(ManageSnapshots, "commit", racing_commit)

    def arm() -> None:
        state["armed"] = True

    return arm
