"""Publishing is by recorded snapshot id: what is approved is exactly what reaches main.

`publish(branch, expected_head)` moves main to `expected_head` only. A branch that keeps
receiving writes after the head was recorded does not carry them into main, a main that moved
is never overwritten, and a main that ends up somewhere unexpected is put back by the Guard.
"""

from __future__ import annotations

import re

import pyarrow as pa
import pytest
from pyiceberg.exceptions import CommitFailedException, CommitStateUnknownException
from pyiceberg.table.update.snapshot import ManageSnapshots

from sqlguard.core.catalog import LakehouseCatalog
from sqlguard.safety import FastForward, Policy, SqlAction
from sqlguard.safety.executor import (
    BranchDiverged,
    BranchHandle,
    DuckDBIcebergAdapter,
    ExecutionError,
    PublishMismatch,
    PublishStateUnknown,
    SparkIcebergAdapter,
)
from sqlguard.safety.guard import Guard, Outcome
from sqlguard.safety.invariants import Invariant
from tests.safety.conftest import ROWS, main_ids, order_ids


def sql(text: str) -> SqlAction:
    return SqlAction(text)


def racer_row() -> pa.Table:
    return pa.table({"id": pa.array([999], pa.int64()), "customer": ["racer"], "amount": [1.0]})


def always_fails() -> Invariant:
    return Invariant(
        "deliberately-wrong", lambda ctx: "wrong on purpose", frozenset({"post_publish"})
    )


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


class Aftermath:
    """What happens around the next snapshot-ref commit (the publish), set up with `arm_*`."""

    def __init__(self, monkeypatch, lake: LakehouseCatalog) -> None:
        self.lake = lake
        self.live = True  # hooks only fire while this is set; `wrap` limits it to publish()
        self.after: list = []  # callables run once, in order, after the commit has landed
        self.before_raise: Exception | None = None  # raised instead of committing
        self.then_raise: Exception | None = None  # raised after committing
        original = ManageSnapshots.commit

        def commit(me):
            if not self.live:
                return original(me)
            if self.before_raise is not None:
                error, self.before_raise = self.before_raise, None
                raise error
            result = original(me)
            after, self.after = self.after, []
            for step in after:
                step()
            if self.then_raise is not None:
                error, self.then_raise = self.then_raise, None
                raise error
            return result

        monkeypatch.setattr(ManageSnapshots, "commit", commit)

    def wrap(self, adapter_class, *args):
        """An `adapter_class(*args)` whose `publish` is the only place the hooks fire."""
        aftermath = self

        class Wrapped(adapter_class):
            def publish(self, branch, expected_head):
                aftermath.live = True
                try:
                    return super().publish(branch, expected_head)
                finally:
                    aftermath.live = False

        aftermath.live = False
        return Wrapped(*args)

    def writer_commits_on_top(self) -> None:
        self.after.append(lambda: self.lake.table("orders").append(racer_row()))

    def main_is_replaced(self, base: int) -> None:
        """Someone rewinds main to `base` and commits their own row: our snapshot is gone."""

        def replace() -> None:
            table = self.lake.table("orders")
            table.manage_snapshots().rollback_to_snapshot(base).commit()
            self.lake.table("orders").append(racer_row())

        self.after.append(replace)


@pytest.fixture
def aftermath(monkeypatch, orders) -> Aftermath:
    return Aftermath(monkeypatch, orders)


@pytest.fixture(autouse=True)
def no_waiting_between_readbacks(monkeypatch):
    monkeypatch.setattr(DuckDBIcebergAdapter, "_READBACK_DELAY_SECONDS", 0)


def recorded_delete(adapter, statement="DELETE FROM orders WHERE id <= 2"):
    branch = adapter.create_branch("orders")
    adapter.apply_sql(branch, statement)
    return adapter.diff(branch).branch


def test_a_writer_committing_on_top_of_the_publish_is_not_a_failure(adapter, orders, aftermath):
    recorded = recorded_delete(adapter)
    aftermath.writer_commits_on_top()
    result = adapter.publish(recorded, recorded.head)
    assert result.changed and result.snapshot_id == recorded.head
    assert adapter.current_snapshot_id("orders") != recorded.head  # their commit is on top
    ids = order_ids(orders)
    assert 999 in ids and 1 not in ids  # both the writer's row and our delete are live


def test_main_replaced_after_the_commit_is_a_mismatch_that_names_where_main_is(
    adapter, orders, aftermath
):
    recorded = recorded_delete(adapter)
    aftermath.main_is_replaced(recorded.base_snapshot_id)
    with pytest.raises(PublishMismatch) as caught:
        adapter.publish(recorded, recorded.head)
    assert caught.value.actual_snapshot_id == adapter.current_snapshot_id("orders")
    assert 999 in order_ids(orders)  # nothing was undone


# -- unknown commit state ----------------------------------------------------------------------

UNKNOWN = [
    pytest.param(CommitStateUnknownException("gateway timeout"), id="commit-state-unknown"),
    pytest.param(ConnectionResetError("connection reset"), id="connection-reset"),
    pytest.param(TimeoutError("timed out"), id="timeout"),
]


@pytest.mark.parametrize("error", UNKNOWN)
def test_an_unknown_commit_that_landed_counts_as_published(adapter, orders, aftermath, error):
    recorded = recorded_delete(adapter)
    aftermath.then_raise = error
    result = adapter.publish(recorded, recorded.head)
    assert result.changed and adapter.current_snapshot_id("orders") == recorded.head


@pytest.mark.parametrize("error", UNKNOWN)
def test_an_unknown_commit_that_reads_as_unchanged_is_still_unknown(
    adapter, orders, aftermath, error
):
    recorded = recorded_delete(adapter)
    aftermath.before_raise = error
    with pytest.raises(PublishStateUnknown, match="may still land"):
        adapter.publish(recorded, recorded.head)
    assert adapter.current_snapshot_id("orders") == recorded.base_snapshot_id


def test_a_commit_that_lands_late_is_why_unchanged_is_not_proof(adapter, orders, aftermath):
    recorded = recorded_delete(adapter)
    aftermath.before_raise = CommitStateUnknownException("gateway timeout")
    with pytest.raises(PublishStateUnknown):
        adapter.publish(recorded, recorded.head)
    # the server finishes applying the request after the client gave up
    orders.table("orders").manage_snapshots().set_current_snapshot(
        snapshot_id=recorded.head
    ).commit()
    assert adapter.current_snapshot_id("orders") == recorded.head


def test_a_rejected_compare_and_swap_is_the_only_proof_of_not_published(adapter, orders, aftermath):
    recorded = recorded_delete(adapter)
    aftermath.before_raise = CommitFailedException("requirement failed")
    with pytest.raises(BranchDiverged):
        adapter.publish(recorded, recorded.head)
    assert adapter.current_snapshot_id("orders") == recorded.base_snapshot_id


class UnreadableAfterPublish(DuckDBIcebergAdapter):
    """Cannot load tables once `dark` is set (the catalog went away after the commit)."""

    dark = False

    def _load(self, table):
        if self.dark:
            raise ConnectionError("catalog unreachable")
        return super()._load(table)


def test_an_unknown_commit_that_cannot_be_read_back_is_state_unknown(orders, aftermath):
    adapter = UnreadableAfterPublish(orders)
    recorded = recorded_delete(adapter)
    aftermath.then_raise = CommitStateUnknownException("gateway timeout")
    aftermath.after.append(lambda: setattr(adapter, "dark", True))
    with pytest.raises(PublishStateUnknown, match="may or may not hold"):
        adapter.publish(recorded, recorded.head)


def test_a_confirmed_commit_stays_a_success_when_the_second_look_fails(orders, aftermath):
    adapter = UnreadableAfterPublish(orders)
    recorded = recorded_delete(adapter)
    aftermath.after.append(lambda: setattr(adapter, "dark", True))
    result = adapter.publish(recorded, recorded.head)  # the commit response confirmed it
    assert result.changed and result.snapshot_id == recorded.head


# -- the Guard never rolls back past someone else's commit ---------------------------------------


def guard_for(orders, audit_path, adapter, **kwargs) -> Guard:
    return Guard(orders, Policy(), audit_path=audit_path, adapter=adapter, **kwargs)


def test_a_writer_on_top_of_the_publish_leaves_a_published_result(
    orders, scope, audit_path, aftermath
):
    base = main_ids(orders)[-1]
    aftermath.writer_commits_on_top()
    result = guard_for(orders, audit_path, aftermath.wrap(DuckDBIcebergAdapter, orders)).submit(
        sql("DELETE FROM orders WHERE id <= 2"), scope
    )

    assert result.outcome is Outcome.PUBLISHED, result.reasons
    assert result.published and not result.rolled_back and not result.needs_attention
    assert main_ids(orders)[-1] != base
    ids = order_ids(orders)
    assert 999 in ids and 1 not in ids and 2 not in ids


def test_failed_verification_with_a_writer_on_top_is_left_alone_and_flagged(
    orders, scope, audit_path, audit, aftermath
):
    aftermath.writer_commits_on_top()
    guard = guard_for(
        orders,
        audit_path,
        aftermath.wrap(DuckDBIcebergAdapter, orders),
        extra_invariants=[always_fails()],
    )
    result = guard.submit(sql("DELETE FROM orders WHERE id <= 2"), scope)

    assert result.outcome is Outcome.ROLLBACK_BLOCKED and result.needs_attention
    assert result.published and not result.rolled_back
    ids = order_ids(orders)
    assert 999 in ids and 1 not in ids  # the writer's commit survives; nothing was undone
    completed = [r for r in audit.read() if r["phase"] == "completed"][-1]
    assert completed["needs_attention"] is True


def test_main_replaced_after_the_publish_needs_attention_and_loses_nothing(
    orders, scope, audit_path, audit, aftermath
):
    base = main_ids(orders)[-1]
    aftermath.main_is_replaced(base)
    result = guard_for(orders, audit_path, aftermath.wrap(DuckDBIcebergAdapter, orders)).submit(
        sql("DELETE FROM orders WHERE id <= 2"), scope
    )

    assert result.outcome is Outcome.ROLLBACK_BLOCKED and result.needs_attention
    assert not result.published and not result.rolled_back
    assert any("needs attention" in r for r in result.reasons)
    assert not any("post-publish check failed" in r for r in result.reasons)  # no check ran
    assert 999 in order_ids(orders)
    assert set(orders.refs("orders")) == {"main"}  # audit branch still cleaned up
    assert [r["phase"] for r in audit.read()] == ["started", "completed"]


@pytest.mark.parametrize("error", UNKNOWN)
def test_unknown_commit_that_landed_is_verified_and_published(
    orders, scope, audit_path, aftermath, error
):
    aftermath.then_raise = error
    result = guard_for(orders, audit_path, aftermath.wrap(DuckDBIcebergAdapter, orders)).submit(
        sql("DELETE FROM orders WHERE id <= 2"), scope
    )
    assert result.outcome is Outcome.PUBLISHED and result.published


@pytest.mark.parametrize("error", UNKNOWN)
def test_unknown_commit_that_reads_as_unchanged_needs_attention(
    orders, scope, audit_path, aftermath, error
):
    base = main_ids(orders)[-1]
    aftermath.before_raise = error
    result = guard_for(orders, audit_path, aftermath.wrap(DuckDBIcebergAdapter, orders)).submit(
        sql("DELETE FROM orders WHERE id <= 2"), scope
    )
    assert result.outcome is Outcome.ROLLBACK_BLOCKED and result.needs_attention
    assert any("may still land" in r for r in result.reasons)
    assert main_ids(orders)[-1] == base and order_ids(orders) == list(range(1, ROWS + 1))


def test_a_late_landing_commit_was_never_reported_as_clean(orders, scope, audit_path, aftermath):
    aftermath.before_raise = CommitStateUnknownException("gateway timeout")
    result = guard_for(orders, audit_path, aftermath.wrap(DuckDBIcebergAdapter, orders)).submit(
        sql("DELETE FROM orders WHERE id <= 2"), scope
    )
    assert result.outcome is Outcome.ROLLBACK_BLOCKED and result.needs_attention
    # the server applies the commit after the Guard gave up: main now holds an unverified change
    table = orders.table("orders")
    on_main = set(main_ids(orders))
    off_main = [s for s in table.snapshots() if s.snapshot_id not in on_main]
    late = max(off_main, key=lambda s: s.sequence_number).snapshot_id  # the rehearsed head
    table.manage_snapshots().set_current_snapshot(snapshot_id=late).commit()
    assert 1 not in order_ids(orders)


def test_unknown_commit_that_cannot_be_read_back_needs_attention(
    orders, scope, audit_path, aftermath
):
    adapter = aftermath.wrap(UnreadableAfterPublish, orders)
    aftermath.then_raise = CommitStateUnknownException("gateway timeout")
    aftermath.after.append(lambda: setattr(adapter, "dark", True))
    result = guard_for(orders, audit_path, adapter).submit(
        sql("DELETE FROM orders WHERE id <= 2"), scope
    )
    assert result.outcome is Outcome.ROLLBACK_BLOCKED and result.needs_attention
    assert any("may or may not hold" in r for r in result.reasons)
    adapter.dark = False
    assert 1 not in order_ids(orders)  # the change did land; the Guard did not claim otherwise


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
        self.fail_on: str | None = None

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
        elif self.fail_on and self.fail_on in text:
            raise RuntimeError("boom")
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


def test_spark_publish_with_a_writer_on_top_keeps_their_commit(
    orders, spark, scope, audit_path, aftermath
):
    aftermath.writer_commits_on_top()
    adapter = aftermath.wrap(SparkIcebergAdapter, orders, spark, "demo")
    result = guard_for(orders, audit_path, adapter).submit(
        sql("DELETE FROM orders WHERE id <= 2"), scope
    )
    assert result.outcome is Outcome.PUBLISHED, result.reasons
    assert 999 in order_ids(orders) and 1 not in order_ids(orders)
    assert not any("rollback_to_snapshot" in s for s in spark.statements)


def test_spark_main_replaced_after_publish_is_left_alone_and_flagged(
    orders, spark, scope, audit_path, aftermath
):
    aftermath.main_is_replaced(main_ids(orders)[-1])
    adapter = aftermath.wrap(SparkIcebergAdapter, orders, spark, "demo")
    result = guard_for(orders, audit_path, adapter).submit(
        sql("DELETE FROM orders WHERE id <= 2"), scope
    )
    assert result.outcome is Outcome.ROLLBACK_BLOCKED and result.needs_attention
    assert 999 in order_ids(orders)
    assert not any("rollback_to_snapshot" in s for s in spark.statements)


def test_spark_failed_verification_with_a_writer_on_top_is_not_rolled_back(
    orders, spark, scope, audit_path, aftermath
):
    aftermath.writer_commits_on_top()
    adapter = aftermath.wrap(SparkIcebergAdapter, orders, spark, "demo")
    guard = guard_for(orders, audit_path, adapter, extra_invariants=[always_fails()])
    result = guard.submit(sql("DELETE FROM orders WHERE id <= 2"), scope)
    assert result.outcome is Outcome.ROLLBACK_BLOCKED and result.needs_attention
    assert 999 in order_ids(orders) and 1 not in order_ids(orders)
    assert not any("rollback_to_snapshot" in s for s in spark.statements)


REFRESH = "REFRESH TABLE `demo`.`db`.`orders`"


def test_spark_cache_is_refreshed_after_main_moves_outside_spark(spark_adapter, spark):
    branch = spark_adapter.create_branch("orders")
    spark_adapter.apply_sql(branch, "DELETE FROM orders WHERE id <= 5")
    recorded = spark_adapter.diff(branch).branch
    before = len(spark.statements)
    spark_adapter.publish(recorded, recorded.head)
    assert spark.statements[before:] == [REFRESH]


def test_spark_cache_is_refreshed_even_when_the_publish_fails(spark_adapter, spark, orders):
    branch = spark_adapter.create_branch("orders")
    spark_adapter.apply_sql(branch, "DELETE FROM orders WHERE id <= 5")
    recorded = spark_adapter.diff(branch).branch
    orders.table("orders").append(racer_row())
    with pytest.raises(BranchDiverged):
        spark_adapter.publish(recorded, recorded.head)
    assert spark.statements[-1] == REFRESH


def test_a_failing_refresh_does_not_undo_a_publish(spark_adapter, spark, orders, caplog):
    branch = spark_adapter.create_branch("orders")
    spark_adapter.apply_sql(branch, "DELETE FROM orders WHERE id <= 5")
    recorded = spark_adapter.diff(branch).branch
    spark.fail_on = "REFRESH TABLE"
    result = spark_adapter.publish(recorded, recorded.head)
    assert result.changed and spark_adapter.current_snapshot_id("orders") == recorded.head
    assert "could not refresh Spark's cache" in caplog.text


def test_an_approved_fast_forward_also_refreshes_the_cache(spark_adapter, spark, orders):
    base = spark_adapter.current_snapshot_id("orders")
    orders.table("orders").manage_snapshots().create_branch(base, "release").commit()
    orders.table("orders").append(racer_row(), branch="release")
    action = FastForward("orders", to="release")
    spark_adapter.apply_maintenance(action, spark_adapter.plan_maintenance(action))
    assert spark.statements[-1] == REFRESH


def test_spark_unknown_commit_that_reads_as_unchanged_needs_attention(
    orders, spark, scope, audit_path, aftermath
):
    aftermath.before_raise = CommitStateUnknownException("gateway timeout")
    adapter = aftermath.wrap(SparkIcebergAdapter, orders, spark, "demo")
    result = guard_for(orders, audit_path, adapter).submit(
        sql("DELETE FROM orders WHERE id <= 2"), scope
    )
    assert result.outcome is Outcome.ROLLBACK_BLOCKED and result.needs_attention
    assert any(st.startswith("REFRESH TABLE") for st in spark.statements)  # cache dropped anyway
