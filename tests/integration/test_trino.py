"""TrinoIcebergAdapter against Trino 484 reading Lakekeeper's warehouse, and the composite.

Locks the Trino behaviours the adapter is built on: branch reads, `$refs`/`$snapshots`, a
rollback that takes no expected snapshot (so the adapter verifies), expiry that has no dry run
(so the adapter only lets Trino run it when it provably removes the approved set), and no way to
write a branch.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pyarrow as pa
import pytest

from sqlguard.core.catalog import LakehouseCatalog
from sqlguard.safety import Policy, SqlAction, TaskScope
from sqlguard.safety.actions import ExpireSnapshots, RemoveOrphanFiles, RollbackToSnapshot
from sqlguard.safety.executor import (
    BranchDiverged,
    DuckDBIcebergAdapter,
    ExecutionError,
    UnsupportedAction,
)
from sqlguard.safety.executor.composite import CompositeAdapter
from sqlguard.safety.executor.trino_iceberg import TrinoIcebergAdapter
from sqlguard.safety.guard import Guard, Outcome
from tests.integration.conftest import ROWS, main_id, order_ids, setting


def extra_row(i: int) -> pa.Table:
    return pa.table(
        {"id": pa.array([i], pa.int64()), "customer": ["z"], "amount": pa.array([float(i)])}
    )


@pytest.fixture
def lake(lakekeeper_lake: LakehouseCatalog) -> LakehouseCatalog:
    return lakekeeper_lake


@pytest.fixture
def trino(lake: LakehouseCatalog) -> Iterator[TrinoIcebergAdapter]:
    adapter = TrinoIcebergAdapter.connect(
        lake,
        host=setting("TRINO_HOST"),
        port=int(setting("TRINO_PORT")),
        trino_catalog=setting("TRINO_CATALOG"),
    )
    yield adapter


def publish_delete(lake: LakehouseCatalog, where: str) -> int:
    """Delete rows through the PyIceberg write engine and publish; returns the new main."""
    writer = DuckDBIcebergAdapter(lake)
    branch = writer.create_branch("orders")
    writer.apply_sql(branch, f"DELETE FROM orders WHERE {where}")
    recorded = writer.diff(branch).branch
    writer.publish(recorded, recorded.head)
    writer.drop_branch(branch)
    return main_id(lake)


# -- reads ----------------------------------------------------------------------------------------


def test_trino_reads_what_pyiceberg_wrote(trino: TrinoIcebergAdapter, lake: LakehouseCatalog):
    table = trino.read("orders")
    assert sorted(table.column("id").to_pylist()) == list(range(1, ROWS + 1))
    assert table.schema.field("id").type == pa.int64()
    assert table.schema.field("customer").type == pa.string()
    assert table.schema.field("amount").type == pa.float64()
    assert sorted(table.column("amount").to_pylist()) == sorted(
        lake.read("orders")["amount"].to_pylist()
    )


def test_trino_reads_old_snapshots_and_branches(trino: TrinoIcebergAdapter, lake: LakehouseCatalog):
    base = main_id(lake)
    writer = DuckDBIcebergAdapter(lake)
    branch = writer.create_branch("orders")
    writer.apply_sql(branch, "DELETE FROM orders WHERE id > 15")

    assert trino.read("orders", base).num_rows == ROWS
    assert trino.read_ref("orders", branch.name).num_rows == 15
    assert trino.read("orders").num_rows == ROWS  # main is untouched by the branch
    assert trino.refs("orders") == lake.refs("orders")
    # Same snapshots; the catalog's own listing order is not the commit order, Trino's is.
    assert {s for s, _ in trino.engine_snapshots("orders")} == {
        s.snapshot_id for s in lake.snapshots("orders")
    }


def test_trino_measures_a_branch_it_did_not_write(
    trino: TrinoIcebergAdapter, lake: LakehouseCatalog
):
    writer = DuckDBIcebergAdapter(lake)
    branch = writer.create_branch("orders")
    writer.apply_sql(branch, "DELETE FROM orders WHERE id > 15")
    diff = trino.diff(branch, key=("id",))
    assert diff.impact.rows_deleted == 5
    assert diff.branch.head == lake.refs("orders")[branch.name]


def test_trino_query_and_a_missing_ref(trino: TrinoIcebergAdapter):
    result = trino.query(
        "SELECT customer, count(*) AS n FROM orders GROUP BY customer ORDER BY customer",
        ["orders"],
    )
    assert result.column("n").to_pylist() == [5, 5, 5, 5]
    with pytest.raises(ExecutionError, match="reference name"):
        trino.read_ref("orders", "nope")


def test_table_changes_lists_what_a_snapshot_added(
    trino: TrinoIcebergAdapter, lake: LakehouseCatalog
):
    first = main_id(lake)
    lake.table("orders").append(extra_row(100))
    changes = trino.changes("orders", first, main_id(lake))
    assert changes.column("id").to_pylist() == [100]
    assert changes.column("_change_type").to_pylist() == ["insert"]


# -- writes are refused ---------------------------------------------------------------------------


def test_writes_are_refused_and_the_guard_denies_them(
    trino: TrinoIcebergAdapter, lake: LakehouseCatalog, tmp_path: Path
):
    base = main_id(lake)
    with pytest.raises(UnsupportedAction):
        trino.query("DELETE FROM orders WHERE id = 1", ["orders"])
    with pytest.raises(UnsupportedAction):
        trino.create_branch("orders")

    guard = Guard(lake, Policy(), audit_path=tmp_path / "audit.jsonl", adapter=trino)
    scope = TaskScope.of(namespaces=[lake.namespace])
    result = guard.submit(SqlAction("DELETE FROM orders WHERE id = 1"), scope, lambda r: True)
    assert result.outcome is Outcome.DENIED
    assert "no isolated rehearsal on trino" in result.reasons
    assert main_id(lake) == base and set(lake.refs("orders")) == {"main"}
    read = guard.submit(SqlAction("SELECT count(*) AS n FROM orders"), scope)
    assert read.outcome is Outcome.READ and read.data.column("n").to_pylist() == [ROWS]


def test_orphan_cleanup_is_not_available(trino: TrinoIcebergAdapter):
    with pytest.raises(UnsupportedAction, match="no dry run"):
        trino.plan_maintenance(RemoveOrphanFiles("orders", older_than=datetime.now(UTC)))
    assert not trino.capabilities.orphan_cleanup


# -- rollback -------------------------------------------------------------------------------------


def test_rollback_moves_main_and_verifies_afterwards(
    trino: TrinoIcebergAdapter, lake: LakehouseCatalog
):
    base = main_id(lake)
    head = publish_delete(lake, "id > 15")

    action = RollbackToSnapshot("orders", snapshot_id=base)
    plan = trino.plan_maintenance(action)
    result = trino.apply_maintenance(action, plan)
    assert (result.snapshot_before, result.snapshot_after) == (head, base)
    assert order_ids(lake) == list(range(1, ROWS + 1))
    assert order_ids(lake) == sorted(trino.read("orders").column("id").to_pylist())


def test_rollback_refuses_before_calling_trino_when_main_is_elsewhere(
    trino: TrinoIcebergAdapter, lake: LakehouseCatalog
):
    base = main_id(lake)
    head = publish_delete(lake, "id > 15")
    with pytest.raises(BranchDiverged):
        trino.rollback("orders", base, expected_current=base)
    assert main_id(lake) == head


def test_rollback_will_not_follow_trino_to_a_branch_head(
    trino: TrinoIcebergAdapter, lake: LakehouseCatalog
):
    """Trino itself would accept this snapshot; the adapter checks main's history first."""
    writer = DuckDBIcebergAdapter(lake)
    branch = writer.create_branch("orders")
    writer.apply_sql(branch, "DELETE FROM orders WHERE id > 15")
    elsewhere = lake.refs("orders")[branch.name]
    base = main_id(lake)
    with pytest.raises(ExecutionError, match="not an ancestor"):
        trino.rollback("orders", elsewhere, expected_current=base)
    assert main_id(lake) == base


def test_a_writer_slipping_in_before_trino_loads_the_table_is_put_back(
    trino: TrinoIcebergAdapter, lake: LakehouseCatalog, monkeypatch: pytest.MonkeyPatch
):
    base = main_id(lake)
    lake.table("orders").append(extra_row(100))
    expected = main_id(lake)

    original = trino._run

    def run_after_a_writer(text: str) -> None:
        lake.table("orders").append(extra_row(777))  # lands between our check and Trino's call
        original(text)

    monkeypatch.setattr(trino, "_run", run_after_a_writer)
    with pytest.raises(BranchDiverged, match="newer snapshot was restored"):
        trino.rollback("orders", base, expected_current=expected)
    ids = order_ids(lake)
    assert 777 in ids and 100 in ids  # the racing writer's commit is live again


# -- expire ---------------------------------------------------------------------------------------


def old_history(lake: LakehouseCatalog) -> tuple[list[int], datetime]:
    """Three snapshots on main that are a few seconds old, and a cutoff after all of them."""
    lake.table("orders").append(extra_row(100))
    lake.table("orders").append(extra_row(101))
    time.sleep(8)
    table = lake.table("orders")
    ids, current = [], table.current_snapshot()
    while current is not None:  # main's lineage, oldest first (the catalog's list order is not it)
        ids.insert(0, current.snapshot_id)
        current = (
            table.snapshot_by_id(current.parent_snapshot_id) if current.parent_snapshot_id else None
        )
    return ids, datetime.now(UTC) - timedelta(seconds=4)


def test_trinos_retention_floor_sends_the_expiry_to_pyiceberg(
    trino: TrinoIcebergAdapter, lake: LakehouseCatalog
):
    ids, cutoff = old_history(lake)
    trino.clock_margin = timedelta(seconds=3)
    action = ExpireSnapshots("orders", older_than=cutoff)
    plan = trino.plan_maintenance(action)
    assert {s.snapshot_id for s in plan.expired} == set(ids[:-1])

    result = trino.apply_maintenance(action, plan)  # Trino says "shorter than the minimum"
    assert set(result.expired_snapshot_ids) == set(ids[:-1])
    assert [s.snapshot_id for s in lake.snapshots("orders")] == [ids[-1]]


def test_trino_expires_exactly_the_approved_snapshots(
    lake: LakehouseCatalog, monkeypatch: pytest.MonkeyPatch
):
    trino = TrinoIcebergAdapter.connect(
        lake,
        host=setting("TRINO_HOST"),
        port=int(setting("TRINO_PORT")),
        trino_catalog=setting("TRINO_CATALOG"),
        session_properties={f"{setting('TRINO_CATALOG')}.expire_snapshots_min_retention": "1s"},
    )
    trino.clock_margin = timedelta(seconds=3)
    statements: list[str] = []
    original = trino._run
    monkeypatch.setattr(trino, "_run", lambda text: (statements.append(text), original(text))[1])

    ids, cutoff = old_history(lake)
    action = ExpireSnapshots("orders", older_than=cutoff)
    plan = trino.plan_maintenance(action)
    assert any("Trino deletes" in w for w in plan.warnings)

    result = trino.apply_maintenance(action, plan)
    assert len(statements) == 1 and "expire_snapshots(retention_threshold" in statements[0]
    assert (
        set(result.expired_snapshot_ids) == set(ids[:-1]) == {s.snapshot_id for s in plan.expired}
    )
    assert [s.snapshot_id for s in lake.snapshots("orders")] == [ids[-1]]
    assert order_ids(lake) == [*range(1, ROWS + 1), 100, 101]  # live data untouched


def retention_trino(lake: LakehouseCatalog) -> TrinoIcebergAdapter:
    """Trino with its 7-day retention floor lowered for this session, so that it would run."""
    trino = TrinoIcebergAdapter.connect(
        lake,
        host=setting("TRINO_HOST"),
        port=int(setting("TRINO_PORT")),
        trino_catalog=setting("TRINO_CATALOG"),
        session_properties={f"{setting('TRINO_CATALOG')}.expire_snapshots_min_retention": "1s"},
    )
    trino.clock_margin = timedelta(seconds=3)
    return trino


def test_trino_itself_expires_a_tag_past_its_max_ref_age(lake: LakehouseCatalog):
    """The reason the adapter keeps Trino away from tables with refs: Iceberg's expiry also
    drops aged refs (and the snapshots only they kept), which no plan lists."""
    ids, cutoff = old_history(lake)
    lake.table("orders").manage_snapshots().create_tag(ids[0], "aged", max_ref_age_ms=1000).commit()
    trino = retention_trino(lake)
    cursor = trino.connection.cursor()
    cursor.execute(
        f'ALTER TABLE "{lake.namespace}"."orders" EXECUTE expire_snapshots(retention_threshold => '
        f"'{max(2, int((datetime.now(UTC) - cutoff).total_seconds()))}s')"
    )
    cursor.fetchall()
    assert "aged" not in lake.refs("orders")
    assert ids[0] not in [s.snapshot_id for s in lake.snapshots("orders")]


def test_a_table_with_a_tag_is_expired_by_pyiceberg_exactly_as_planned(
    lake: LakehouseCatalog, monkeypatch: pytest.MonkeyPatch
):
    ids, cutoff = old_history(lake)
    lake.table("orders").manage_snapshots().create_tag(ids[0], "aged", max_ref_age_ms=1000).commit()
    trino = retention_trino(lake)
    statements: list[str] = []
    original = trino._run
    monkeypatch.setattr(trino, "_run", lambda text: (statements.append(text), original(text))[1])

    action = ExpireSnapshots("orders", older_than=cutoff)
    plan = trino.plan_maintenance(action)
    assert not any("Trino deletes" in w for w in plan.warnings)
    result = trino.apply_maintenance(action, plan)

    assert statements == []  # never sent to Trino
    assert set(result.expired_snapshot_ids) == {s.snapshot_id for s in plan.expired}
    assert lake.refs("orders")["aged"] == ids[0]  # the tag and its snapshot survived
    assert ids[0] in [s.snapshot_id for s in lake.snapshots("orders")]


# -- composite ------------------------------------------------------------------------------------


def test_trino_reads_and_pyiceberg_writes_through_the_guard(
    trino: TrinoIcebergAdapter, lake: LakehouseCatalog, tmp_path: Path
):
    composite = CompositeAdapter(read=trino, write=DuckDBIcebergAdapter(lake))
    assert composite.capabilities.branch_write and composite.capabilities.changelog
    guard = Guard(lake, Policy(), audit_path=tmp_path / "audit.jsonl", adapter=composite)
    scope = TaskScope.of(namespaces=[lake.namespace])

    result = guard.submit(SqlAction("DELETE FROM orders WHERE id = 20"), scope, lambda r: False)
    assert result.outcome is Outcome.PUBLISHED and result.published
    assert order_ids(lake) == list(range(1, ROWS))
    assert order_ids(lake) == sorted(trino.read("orders").column("id").to_pylist())
    assert set(lake.refs("orders")) == {"main"}
