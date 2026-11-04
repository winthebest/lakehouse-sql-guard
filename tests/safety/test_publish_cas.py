"""Publishing is by recorded snapshot id: what is approved is exactly what reaches main.

`publish(branch, expected_head)` moves main to `expected_head` only. A branch that keeps
receiving writes after the head was recorded does not carry them into main, a main that moved
is never overwritten, and a main that ends up somewhere unexpected is put back by the Guard.
"""

from __future__ import annotations

import re

import pyarrow as pa
import pytest
from pyiceberg.table.update.snapshot import ManageSnapshots

from sqlguard.core.catalog import LakehouseCatalog
from sqlguard.safety import Policy, SqlAction
from sqlguard.safety.executor import (
    BranchDiverged,
    BranchHandle,
    DuckDBIcebergAdapter,
    ExecutionError,
    PublishMismatch,
    SparkIcebergAdapter,
)
from sqlguard.safety.guard import Guard, Outcome
from tests.safety.conftest import ROWS, main_ids, order_ids


def sql(text: str) -> SqlAction:
    return SqlAction(text)


def racer_row() -> pa.Table:
    return pa.table({"id": pa.array([999], pa.int64()), "customer": ["racer"], "amount": [1.0]})


def branch_head(lake: LakehouseCatalog, branch: BranchHandle) -> int:
    return lake.table("orders").refs()[branch.name].snapshot_id


@pytest.fixture
def adapter(orders: LakehouseCatalog) -> DuckDBIcebergAdapter:
    return DuckDBIcebergAdapter(orders)


# -- the adapter ---------------------------------------------------------------------------------


def test_diff_records_the_head_it_measured(adapter, orders):
    branch = adapter.create_branch("orders")
    assert branch.head_snapshot_id == branch.base_snapshot_id
    adapter.apply_sql(branch, "DELETE FROM orders WHERE id <= 5")
    recorded = adapter.diff(branch).branch
    assert recorded.head_snapshot_id == branch_head(orders, branch) != branch.base_snapshot_id
    assert (recorded.table, recorded.name, recorded.base_snapshot_id) == (
        branch.table,
        branch.name,
        branch.base_snapshot_id,
    )


def test_a_write_to_the_branch_after_recording_is_not_published(adapter, orders):
    branch = adapter.create_branch("orders")
    adapter.apply_sql(branch, "DELETE FROM orders WHERE id <= 5")
    recorded = adapter.diff(branch).branch

    adapter.apply_sql(branch, "INSERT INTO orders VALUES (555, 'late', 1.0)")  # not approved
    later = branch_head(orders, branch)
    assert later != recorded.head

    result = adapter.publish(recorded, recorded.head)
    assert result.changed and result.snapshot_id == recorded.head
    assert adapter.current_snapshot_id("orders") == recorded.head
    assert order_ids(orders) == list(range(6, ROWS + 1))  # no 555
    assert branch_head(orders, branch) == later  # the branch itself was left alone


def test_the_guard_publishes_the_head_the_approver_saw(make_guard, orders, scope):
    """What an approver adds to the branch after the rehearsal never reaches main."""

    def approve_after_writing_more(request) -> bool:
        [name] = [r for r in orders.refs("orders") if r.startswith("sqlguard-")]
        handle = BranchHandle("db.orders", name, main_ids(orders)[-1])
        DuckDBIcebergAdapter(orders).apply_sql(
            handle, "INSERT INTO orders VALUES (555, 'late', 1.0)"
        )
        return True

    result = make_guard().submit(
        sql("DELETE FROM orders WHERE id IS NOT NULL"), scope, approve_after_writing_more
    )
    assert result.outcome is Outcome.PUBLISHED, result.reasons
    assert order_ids(orders) == []  # the approved change, without the late insert


def test_publishing_the_base_changes_nothing(adapter, orders):
    branch = adapter.create_branch("orders")
    result = adapter.publish(branch, branch.base_snapshot_id)
    assert not result.changed and adapter.current_snapshot_id("orders") == branch.base_snapshot_id


def test_main_moving_since_the_branch_was_cut_is_a_divergence(adapter, orders):
    branch = adapter.create_branch("orders")
    adapter.apply_sql(branch, "DELETE FROM orders WHERE id = 1")
    recorded = adapter.diff(branch).branch
    orders.table("orders").append(racer_row())
    moved = adapter.current_snapshot_id("orders")

    with pytest.raises(BranchDiverged):
        adapter.publish(recorded, recorded.head)
    assert adapter.current_snapshot_id("orders") == moved
    assert 1 in order_ids(orders) and 999 in order_ids(orders)


def test_a_commit_landing_during_publish_is_a_divergence(adapter, orders, race_on_next_commit):
    branch = adapter.create_branch("orders")
    adapter.apply_sql(branch, "DELETE FROM orders WHERE id = 1")
    recorded = adapter.diff(branch).branch
    race_on_next_commit()
    with pytest.raises(BranchDiverged):
        adapter.publish(recorded, recorded.head)
    assert 1 in order_ids(orders) and 999 in order_ids(orders)


def test_a_snapshot_that_does_not_descend_from_the_base_is_refused(adapter, orders):
    first = adapter.current_snapshot_id("orders")
    orders.table("orders").append(racer_row())  # main is now one ahead of `first`
    branch = adapter.create_branch("orders")
    for stranger in (first, 123456789):
        with pytest.raises(ExecutionError, match="does not descend"):
            adapter.publish(branch, stranger)
    assert adapter.current_snapshot_id("orders") == branch.base_snapshot_id


@pytest.fixture
def land_a_writer_after_the_next_publish(monkeypatch, orders):
    """Arm with `arm()`: a second writer's commit lands right after the next snapshot-ref commit."""
    state = {"armed": False}
    original = ManageSnapshots.commit

    def commit_then_racer(self):
        result = original(self)
        if state["armed"]:
            state["armed"] = False
            orders.table("orders").append(racer_row())
        return result

    monkeypatch.setattr(ManageSnapshots, "commit", commit_then_racer)

    def arm() -> None:
        state["armed"] = True

    return arm


def test_main_ending_up_elsewhere_is_reported_with_where_it_is(
    adapter, orders, land_a_writer_after_the_next_publish
):
    branch = adapter.create_branch("orders")
    adapter.apply_sql(branch, "DELETE FROM orders WHERE id = 1")
    recorded = adapter.diff(branch).branch
    land_a_writer_after_the_next_publish()
    with pytest.raises(PublishMismatch) as caught:
        adapter.publish(recorded, recorded.head)
    assert caught.value.actual_snapshot_id == adapter.current_snapshot_id("orders")
    assert caught.value.actual_snapshot_id != recorded.head


# -- the Guard undoes a publish that did not land on the approved snapshot ----------------------


class ArmedDuckDB(DuckDBIcebergAdapter):
    def __init__(self, catalog, arm):
        super().__init__(catalog)
        self._arm = arm

    def publish(self, branch, expected_head):
        self._arm()
        return super().publish(branch, expected_head)


def test_a_post_publish_mismatch_rolls_main_back_to_the_base(
    orders, scope, audit, audit_path, land_a_writer_after_the_next_publish
):
    base = main_ids(orders)[-1]
    guard = Guard(
        orders,
        Policy(),
        audit_path=audit_path,
        adapter=ArmedDuckDB(orders, land_a_writer_after_the_next_publish),
    )
    result = guard.submit(sql("DELETE FROM orders WHERE id <= 2"), scope)

    assert result.outcome is Outcome.ROLLED_BACK, result.reasons
    assert result.rolled_back and not result.published and result.executed
    assert guard.adapter.current_snapshot_id("orders") == base
    assert order_ids(orders) == list(range(1, ROWS + 1))
    assert result.snapshots_after == {"db.orders": base}
    assert any("post-publish check failed" in r for r in result.reasons)
    assert set(orders.refs("orders")) == {"main"}  # audit branch dropped
    completed = [r for r in audit.read() if r["phase"] == "completed"][-1]
    assert completed["outcome"] == "rolled_back" and completed["published"] is False


# -- the Spark adapter publishes through PyIceberg, not a Spark procedure ----------------------


class FakeFrame:
    def __init__(self, rows):
        self._rows = rows

    def collect(self):
        return self._rows


class FakeSpark:
    """Just enough Spark: branch DDL and DML are replayed on the table with PyIceberg."""

    def __init__(self, lake: LakehouseCatalog) -> None:
        self.lake = lake
        self.statements: list[str] = []

    def sql(self, text: str) -> FakeFrame:
        self.statements.append(text)
        table = self.lake.table("orders")
        if match := re.search(r"CREATE BRANCH `([^`]+)` AS OF VERSION (\d+)", text):
            table.manage_snapshots().create_branch(int(match.group(2)), match.group(1)).commit()
        elif match := re.search(r"DELETE FROM .*branch_([\w-]+)` WHERE id <= (\d+)", text):
            name, limit = match.group(1), int(match.group(2))
            head = table.refs()[name].snapshot_id
            rows = self.lake.read("orders", head)
            keep = rows.filter(pa.compute.greater(rows["id"], limit))
            table.overwrite(keep, branch=name)
        elif match := re.search(r"DROP BRANCH IF EXISTS `([^`]+)`", text):
            if match.group(1) in table.refs():
                table.manage_snapshots().remove_branch(match.group(1)).commit()
        elif "rollback_to_snapshot" in text:
            target = int(re.search(r"snapshot_id => (\d+)", text).group(1))
            previous = table.current_snapshot().snapshot_id
            table.manage_snapshots().rollback_to_snapshot(target).commit()
            return FakeFrame([(previous, target)])
        return FakeFrame([])


@pytest.fixture
def spark(orders) -> FakeSpark:
    return FakeSpark(orders)


@pytest.fixture
def spark_adapter(orders, spark) -> SparkIcebergAdapter:
    return SparkIcebergAdapter(orders, spark, "demo")


def test_spark_publishes_the_recorded_head_through_pyiceberg(spark_adapter, spark, orders):
    branch = spark_adapter.create_branch("orders")
    spark_adapter.apply_sql(branch, "DELETE FROM orders WHERE id <= 5")
    recorded = spark_adapter.diff(branch).branch
    orders.table("orders").append(racer_row(), branch=branch.name)  # written after recording

    result = spark_adapter.publish(recorded, recorded.head)
    assert result.snapshot_id == recorded.head == spark_adapter.current_snapshot_id("orders")
    assert 999 not in order_ids(orders)
    assert not any("fast_forward" in s.lower() for s in spark.statements)


def test_spark_publish_refuses_a_moved_main(spark_adapter, orders):
    branch = spark_adapter.create_branch("orders")
    spark_adapter.apply_sql(branch, "DELETE FROM orders WHERE id <= 5")
    recorded = spark_adapter.diff(branch).branch
    orders.table("orders").append(racer_row())
    with pytest.raises(BranchDiverged):
        spark_adapter.publish(recorded, recorded.head)


def test_spark_post_publish_mismatch_rolls_main_back(
    orders, spark_adapter, spark, scope, audit_path, land_a_writer_after_the_next_publish
):
    base = main_ids(orders)[-1]
    arm = land_a_writer_after_the_next_publish

    class ArmedSpark(SparkIcebergAdapter):
        def publish(self, branch, expected_head):
            arm()
            return super().publish(branch, expected_head)

    adapter = ArmedSpark(orders, spark, "demo")
    guard = Guard(orders, Policy(), audit_path=audit_path, adapter=adapter)
    result = guard.submit(sql("DELETE FROM orders WHERE id <= 2"), scope)

    assert result.outcome is Outcome.ROLLED_BACK, result.reasons
    assert not result.published and result.rolled_back
    assert adapter.current_snapshot_id("orders") == base
    assert order_ids(orders) == list(range(1, ROWS + 1))
    assert any("rollback_to_snapshot" in s for s in spark.statements)
