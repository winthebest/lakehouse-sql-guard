"""CompositeAdapter: Trino reads and verifies, a write engine writes, the Guard sees one adapter."""

from __future__ import annotations

import dataclasses

import pytest

from sqlguard.core.catalog import LakehouseCatalog
from sqlguard.core.findings import Decision
from sqlguard.safety import Policy, SqlAction, TaskScope
from sqlguard.safety.actions import ExpireSnapshots, RollbackToSnapshot
from sqlguard.safety.executor import Capabilities, DuckDBIcebergAdapter, UnsupportedAction
from sqlguard.safety.executor.composite import CompositeAdapter, combined_capabilities
from sqlguard.safety.executor.trino_iceberg import TrinoIcebergAdapter
from sqlguard.safety.guard import Guard, Outcome
from tests.safety.conftest import ROWS, main_ids, order_ids
from tests.safety.test_trino_adapter import FakeTrino


@pytest.fixture
def trino(orders: LakehouseCatalog) -> FakeTrino:
    return FakeTrino(orders)


@pytest.fixture
def reader(orders: LakehouseCatalog, trino: FakeTrino) -> TrinoIcebergAdapter:
    return TrinoIcebergAdapter(orders, trino, "lk")


@pytest.fixture
def writer(orders: LakehouseCatalog) -> DuckDBIcebergAdapter:
    return DuckDBIcebergAdapter(orders)


@pytest.fixture
def composite(reader: TrinoIcebergAdapter, writer: DuckDBIcebergAdapter) -> CompositeAdapter:
    return CompositeAdapter(read=reader, write=writer)


def test_capabilities_are_the_union_by_role(composite: CompositeAdapter) -> None:
    assert composite.engine == "trino+duckdb"
    assert composite.capabilities == Capabilities(
        branch_write=True,
        publish=True,
        rollback=True,
        expire=True,
        orphan_cleanup=True,  # the write engine's: Trino has no orphan dry run
        changelog=True,  # the read engine's
    )


def test_write_abilities_never_come_from_the_read_engine() -> None:
    read = Capabilities.full()
    write = Capabilities(rollback=True)
    merged = combined_capabilities(read, write)
    assert dataclasses.asdict(merged) == {
        "branch_write": False,
        "publish": False,
        "rollback": True,
        "expire": False,
        "orphan_cleanup": False,
        "changelog": True,
    }


def test_an_adapter_cannot_be_composed_with_itself(writer: DuckDBIcebergAdapter) -> None:
    with pytest.raises(ValueError):
        CompositeAdapter(read=writer, write=writer)


def test_reads_go_to_trino_and_writes_to_the_write_engine(
    composite: CompositeAdapter, orders: LakehouseCatalog, trino: FakeTrino
) -> None:
    base = composite.current_snapshot_id("orders")
    assert composite.read("orders").num_rows == ROWS
    assert trino.statements == ['SELECT * FROM "lk"."db"."orders"']

    branch = composite.create_branch("orders")
    composite.apply_sql(branch, "DELETE FROM orders WHERE id > 15")
    assert composite.current_snapshot_id("orders") == base
    assert trino.altered() == [] and len(trino.statements) == 1  # Trino saw none of the writing

    diff = composite.diff(branch, key=("id",))
    assert diff.impact.rows_deleted == 5
    assert any("FOR VERSION AS OF" in s for s in trino.statements)  # measured through Trino

    published = composite.publish(diff.branch, diff.branch.head)
    assert published.changed and published.snapshot_id == diff.branch.head
    composite.drop_branch(branch)
    assert order_ids(orders) == list(range(1, 16))
    assert set(orders.refs("orders")) == {"main"}


def test_maintenance_goes_to_the_write_engine(
    composite: CompositeAdapter, orders: LakehouseCatalog, trino: FakeTrino
) -> None:
    from tests.safety.test_trino_adapter import row

    first = composite.current_snapshot_id("orders")
    orders.table("orders").append(row(100))
    action = RollbackToSnapshot("orders", snapshot_id=first)
    composite.apply_maintenance(action, composite.plan_maintenance(action))
    assert composite.current_snapshot_id("orders") == first
    composite.plan_maintenance(ExpireSnapshots("orders", snapshot_ids=(first,)))
    assert trino.statements == []  # rollback and planning never touched Trino


@pytest.fixture
def guard(composite: CompositeAdapter, orders: LakehouseCatalog, audit_path) -> Guard:
    return Guard(orders, Policy(), audit_path=audit_path, adapter=composite)


def test_the_guard_rehearses_with_the_write_engine_and_measures_with_trino(
    guard: Guard, orders: LakehouseCatalog, trino: FakeTrino
) -> None:
    scope = TaskScope.of(namespaces=["db"])
    before = main_ids(orders)
    result = guard.submit(SqlAction("DELETE FROM orders WHERE id = 20"), scope, lambda r: True)
    assert result.outcome is Outcome.PUBLISHED and result.published
    assert order_ids(orders) == list(range(1, ROWS))
    assert main_ids(orders)[: len(before)] == before and len(main_ids(orders)) > len(before)
    assert set(orders.refs("orders")) == {"main"}  # the audit branch was dropped
    assert any("FOR VERSION AS OF" in s for s in trino.statements)
    assert trino.altered() == []


def test_a_trino_only_guard_denies_writes(
    reader: TrinoIcebergAdapter, orders: LakehouseCatalog, audit_path, trino: FakeTrino
) -> None:
    guard = Guard(orders, Policy(), audit_path=audit_path, adapter=reader)
    scope = TaskScope.of(namespaces=["db"])
    result = guard.submit(SqlAction("DELETE FROM orders WHERE id = 1"), scope, lambda r: True)
    assert result.outcome is Outcome.DENIED and result.decision is Decision.DENY
    assert "no isolated rehearsal on trino" in result.reasons
    assert order_ids(orders) == list(range(1, ROWS + 1))
    assert set(orders.refs("orders")) == {"main"}
    assert trino.statements == []


def test_reads_through_the_guard_use_trino(guard: Guard, trino: FakeTrino) -> None:
    trino.canned.append(("COUNT(*)", ([("n", "bigint")], [[ROWS]])))
    result = guard.submit(
        SqlAction("SELECT count(*) AS n FROM orders"), TaskScope.of(namespaces=["db"])
    )
    assert result.outcome is Outcome.READ
    assert result.data.column("n").to_pylist() == [ROWS]
    assert trino.statements[-1] == 'SELECT COUNT(*) AS n FROM "lk"."db"."orders"'


def test_the_change_feed_comes_from_the_read_engine(
    composite: CompositeAdapter, trino: FakeTrino, writer: DuckDBIcebergAdapter
) -> None:
    trino.canned.append(("table_changes", ([("id", "bigint")], [[7]])))
    assert composite.changes("orders", 1, 2).column("id").to_pylist() == [7]
    with pytest.raises(UnsupportedAction):
        CompositeAdapter(read=writer, write=DuckDBIcebergAdapter(writer.catalog)).changes(
            "orders", 1, 2
        )
