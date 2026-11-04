"""The publish contract on real REST catalogs: Lakekeeper, the REST fixture and Polaris.

Each test runs once per catalog. The write engine is the PyIceberg-backed adapter, so what is
under test is the catalog's behaviour: branches, branch writes that leave main alone, and the
compare-and-swap that makes "publish exactly the snapshot that was approved" safe.
"""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pytest
from pyiceberg.exceptions import CommitFailedException
from pyiceberg.table.update.snapshot import ManageSnapshots

from sqlguard.core.catalog import LakehouseCatalog
from sqlguard.safety import Policy, SqlAction, TaskScope
from sqlguard.safety.actions import ExpireSnapshots, RollbackToSnapshot
from sqlguard.safety.executor import (
    BranchDiverged,
    DuckDBIcebergAdapter,
)
from sqlguard.safety.guard import Guard, Outcome
from tests.integration.conftest import ROWS, main_id, order_ids


@pytest.fixture
def adapter(lake: LakehouseCatalog) -> DuckDBIcebergAdapter:
    return DuckDBIcebergAdapter(lake)


def extra_row(i: int) -> pa.Table:
    return pa.table(
        {"id": pa.array([i], pa.int64()), "customer": ["z"], "amount": pa.array([float(i)])}
    )


def test_create_branch_leaves_main_alone(adapter: DuckDBIcebergAdapter, lake: LakehouseCatalog):
    base = main_id(lake)
    branch = adapter.create_branch("orders")
    refs = lake.refs("orders")
    assert refs == {"main": base, branch.name: base}
    assert branch.base_snapshot_id == base and branch.head == base


def test_a_branch_write_never_touches_main(adapter: DuckDBIcebergAdapter, lake: LakehouseCatalog):
    base = main_id(lake)
    branch = adapter.create_branch("orders")
    adapter.apply_sql(branch, "DELETE FROM orders WHERE id > 15")

    assert main_id(lake) == base and order_ids(lake) == list(range(1, ROWS + 1))
    head = lake.refs("orders")[branch.name]
    assert head != base
    assert lake.read("orders", head).num_rows == 15
    diff = adapter.diff(branch, key=("id",))
    assert diff.impact.rows_deleted == 5 and diff.branch.head == head


def test_publish_moves_main_to_exactly_the_approved_snapshot(
    adapter: DuckDBIcebergAdapter, lake: LakehouseCatalog
):
    base = main_id(lake)
    branch = adapter.create_branch("orders")
    adapter.apply_sql(branch, "DELETE FROM orders WHERE id > 15")
    recorded = adapter.diff(branch).branch

    result = adapter.publish(recorded, recorded.head)
    assert result.changed and result.previous_snapshot_id == base
    assert result.snapshot_id == recorded.head == main_id(lake)
    assert order_ids(lake) == list(range(1, 16))
    parents = {s.snapshot_id: s.parent_id for s in lake.snapshots("orders")}
    assert parents[main_id(lake)] is not None  # the branch commits are part of main's history

    adapter.drop_branch(branch)
    assert set(lake.refs("orders")) == {"main"}


def test_publish_leaves_out_what_the_branch_got_after_the_approval(
    adapter: DuckDBIcebergAdapter, lake: LakehouseCatalog
):
    branch = adapter.create_branch("orders")
    adapter.apply_sql(branch, "DELETE FROM orders WHERE id > 15")
    recorded = adapter.diff(branch).branch
    adapter.apply_sql(branch, "DELETE FROM orders WHERE id > 5")  # after the measurement
    assert lake.refs("orders")[branch.name] != recorded.head

    adapter.publish(recorded, recorded.head)
    assert main_id(lake) == recorded.head
    assert order_ids(lake) == list(range(1, 16))  # not the 5-row table the branch holds now


def test_publish_after_main_moved_is_refused(adapter: DuckDBIcebergAdapter, lake: LakehouseCatalog):
    branch = adapter.create_branch("orders")
    adapter.apply_sql(branch, "DELETE FROM orders WHERE id > 15")
    recorded = adapter.diff(branch).branch
    lake.table("orders").append(extra_row(500))  # someone else commits to main
    moved = main_id(lake)

    with pytest.raises(BranchDiverged):
        adapter.publish(recorded, recorded.head)
    assert main_id(lake) == moved and 500 in order_ids(lake)


def test_the_catalog_rejects_a_commit_built_on_a_stale_main(lake: LakehouseCatalog):
    """The server-side compare-and-swap, with no adapter in between."""
    branch_base = main_id(lake)
    lake.table("orders").manage_snapshots().create_branch(branch_base, "stale-test").commit()
    lake.table("orders").append(extra_row(600), branch="stale-test")
    head = lake.refs("orders")["stale-test"]

    stale = lake.table("orders")  # loaded while main is at branch_base
    lake.table("orders").append(extra_row(601))  # main moves on
    with pytest.raises(CommitFailedException):
        stale.manage_snapshots().set_current_snapshot(snapshot_id=head).commit()
    assert main_id(lake) != head


@pytest.fixture
def race_on_next_commit(monkeypatch: pytest.MonkeyPatch, lake: LakehouseCatalog):
    """Arm with `arm()`: the next ref commit is preceded by a concurrent append on main."""
    state = {"armed": False}
    original = ManageSnapshots.commit

    def racing_commit(self):
        if state["armed"]:
            state["armed"] = False
            lake.table("orders").append(extra_row(999))  # a second handle: a second writer
        return original(self)

    monkeypatch.setattr(ManageSnapshots, "commit", racing_commit)

    def arm() -> None:
        state["armed"] = True

    return arm


def test_a_writer_racing_the_publish_commit_is_not_overwritten(
    adapter: DuckDBIcebergAdapter, lake: LakehouseCatalog, race_on_next_commit
):
    branch = adapter.create_branch("orders")
    adapter.apply_sql(branch, "DELETE FROM orders WHERE id > 15")
    recorded = adapter.diff(branch).branch

    race_on_next_commit()
    with pytest.raises(BranchDiverged):
        adapter.publish(recorded, recorded.head)
    assert 999 in order_ids(lake) and main_id(lake) != recorded.head


def test_rollback_checks_where_main_is(adapter: DuckDBIcebergAdapter, lake: LakehouseCatalog):
    base = main_id(lake)
    branch = adapter.create_branch("orders")
    adapter.apply_sql(branch, "DELETE FROM orders WHERE id > 15")
    recorded = adapter.diff(branch).branch
    adapter.publish(recorded, recorded.head)

    with pytest.raises(BranchDiverged):
        adapter.rollback("orders", base, expected_current=base)  # main is not at base any more
    assert main_id(lake) == recorded.head

    adapter.rollback("orders", base, expected_current=recorded.head)
    assert main_id(lake) == base and order_ids(lake) == list(range(1, ROWS + 1))


def test_a_writer_racing_the_rollback_commit_is_not_discarded(
    adapter: DuckDBIcebergAdapter, lake: LakehouseCatalog, race_on_next_commit
):
    base = main_id(lake)
    lake.table("orders").append(extra_row(700))
    current = main_id(lake)

    race_on_next_commit()
    with pytest.raises(BranchDiverged):
        adapter.rollback("orders", base, expected_current=current)
    assert 999 in order_ids(lake) and 700 in order_ids(lake)


def test_snapshots_expire_through_the_catalog(
    adapter: DuckDBIcebergAdapter, lake: LakehouseCatalog
):
    first = main_id(lake)
    lake.table("orders").append(extra_row(800))
    action = ExpireSnapshots("orders", snapshot_ids=(first,))
    plan = adapter.plan_maintenance(action)
    result = adapter.apply_maintenance(action, plan)
    assert result.expired_snapshot_ids == (first,)
    assert first not in [s.snapshot_id for s in lake.snapshots("orders")]

    rollback = RollbackToSnapshot("orders", snapshot_id=first)
    with pytest.raises(Exception, match="unknown snapshot"):
        adapter.plan_maintenance(rollback)


def test_the_guard_publishes_a_small_delete_and_holds_a_big_one(
    adapter: DuckDBIcebergAdapter, lake: LakehouseCatalog, tmp_path: Path
):
    guard = Guard(lake, Policy(), audit_path=tmp_path / "audit.jsonl", adapter=adapter)
    scope = TaskScope.of(namespaces=[lake.namespace])
    approvals: list[object] = []

    def approver(request: object) -> bool:
        approvals.append(request)
        return False

    result = guard.submit(SqlAction("DELETE FROM orders WHERE id = 20"), scope, approver)
    assert result.outcome is Outcome.PUBLISHED and result.published
    assert order_ids(lake) == list(range(1, ROWS))

    before = main_id(lake)
    held = guard.submit(SqlAction("DELETE FROM orders"), scope, approver)
    assert not held.published and approvals  # a human was asked, and declined
    assert main_id(lake) == before
    assert set(lake.refs("orders")) == {"main"}  # the rehearsal branch is gone
