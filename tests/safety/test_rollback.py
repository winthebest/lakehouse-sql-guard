"""A change that passes the pre-publish checks but fails verification on main is undone."""

from __future__ import annotations

import pyarrow as pa
import pytest

from sqlguard.core.findings import Decision
from sqlguard.safety import SqlAction
from sqlguard.safety.executor import DuckDBIcebergAdapter, ExecutionError
from sqlguard.safety.guard import Outcome
from sqlguard.safety.invariants import Invariant
from tests.safety.conftest import ROWS, main_ids, order_ids

POST_ONLY = frozenset({"post_publish"})
PRE_ONLY = frozenset({"pre_publish"})


def sql(text: str) -> SqlAction:
    return SqlAction(text)


def always_fails(phases=POST_ONLY) -> Invariant:
    return Invariant("deliberately-wrong", lambda ctx: "this invariant is wrong on purpose", phases)


def test_failed_post_publish_check_rolls_main_back(make_guard, orders, scope, audit):
    before_ids = main_ids(orders)
    guard = make_guard(extra_invariants=[always_fails()])
    result = guard.submit(sql("DELETE FROM orders WHERE id <= 2"), scope)

    assert result.outcome is Outcome.ROLLED_BACK
    assert result.executed and result.rolled_back and not result.published
    assert order_ids(orders) == list(range(1, ROWS + 1))  # data exactly as before
    assert main_ids(orders) == before_ids
    assert result.snapshots_after == result.snapshots_before
    assert [(r.phase, r.passed) for r in result.invariants] == [("post_publish", False)]
    assert set(orders.refs("orders")) == {"main"}

    record = audit.read()[-1]
    assert record["outcome"] == "rolled_back" and record["rolled_back"] is True
    assert record["published"] is False
    assert any("deliberately-wrong" in r for r in record["reasons"])


def test_rolled_back_snapshots_stay_in_metadata_for_inspection(make_guard, orders, scope):
    guard = make_guard(extra_invariants=[always_fails()])
    guard.submit(sql("DELETE FROM orders WHERE id <= 2"), scope)
    assert (
        len(orders.table("orders").snapshots()) > 1
    )  # rollback moved the pointer, deleted nothing


def test_phase_selection_is_respected(make_guard, orders, scope):
    only_pre = make_guard(extra_invariants=[always_fails(PRE_ONLY)])
    blocked = only_pre.submit(sql("DELETE FROM orders WHERE id = 1"), scope)
    assert blocked.outcome is Outcome.INVARIANT_FAILED and not blocked.published
    assert order_ids(orders) == list(range(1, ROWS + 1))

    passes_pre = make_guard(extra_invariants=[Invariant("ok", lambda ctx: None, PRE_ONLY)])
    ran = passes_pre.submit(sql("DELETE FROM orders WHERE id = 1"), scope)
    assert ran.outcome is Outcome.PUBLISHED
    assert [r.phase for r in ran.invariants] == ["pre_publish"]


def test_post_publish_check_sees_the_state_that_actually_landed(make_guard, orders, scope):
    seen = {}

    def inspect(ctx):
        seen["before"], seen["after"] = ctx.before.num_rows, ctx.after.num_rows
        return None

    make_guard(extra_invariants=[Invariant("spy", inspect, POST_ONLY)]).submit(
        sql("DELETE FROM orders WHERE id <= 2"), scope
    )
    assert seen == {"before": ROWS, "after": ROWS - 2}


def test_an_invariant_that_crashes_fails_closed(make_guard, orders, scope):
    def crash(ctx):
        raise RuntimeError("bug in the invariant")

    pre = make_guard(extra_invariants=[Invariant("crash", crash, PRE_ONLY)])
    assert pre.submit(sql("DELETE FROM orders WHERE id = 1"), scope).outcome is (
        Outcome.INVARIANT_FAILED
    )
    post = make_guard(extra_invariants=[Invariant("crash", crash, POST_ONLY)])
    assert post.submit(sql("DELETE FROM orders WHERE id = 1"), scope).outcome is Outcome.ROLLED_BACK
    assert order_ids(orders) == list(range(1, ROWS + 1))


def test_verification_that_cannot_run_rolls_back(make_guard, orders, scope):
    class FlakyRead(DuckDBIcebergAdapter):
        def read(self, table, snapshot_id=None):
            if snapshot_id is not None and snapshot_id != self.start:
                raise ExecutionError("storage unavailable")
            return super().read(table, snapshot_id)

    adapter = FlakyRead(orders)
    adapter.start = orders.table("orders").current_snapshot().snapshot_id
    # branch reads go through the catalog directly, only the post-publish read hits the adapter
    guard = make_guard(adapter=adapter)
    result = guard.submit(sql("DELETE FROM orders WHERE id = 1"), scope)
    assert result.outcome is Outcome.ROLLED_BACK
    assert any("verification crashed" in r for r in result.reasons)
    assert order_ids(orders) == list(range(1, ROWS + 1))


def test_main_that_moved_after_the_publish_is_not_rolled_back(make_guard, orders, scope):
    def concurrent_writer(ctx):
        orders.table("orders").append(
            pa.table(
                {
                    "id": pa.array([888], pa.int64()),
                    "customer": ["late"],
                    "amount": pa.array([1.0]),
                }
            )
        )
        return "fails after someone else wrote on top"

    guard = make_guard(extra_invariants=[Invariant("late-writer", concurrent_writer, POST_ONLY)])
    result = guard.submit(sql("DELETE FROM orders WHERE id = 1"), scope)
    assert result.outcome is Outcome.ROLLBACK_BLOCKED
    assert result.published and not result.rolled_back
    assert 888 in order_ids(orders)  # the other writer's data is not discarded
    assert any("not rolled back automatically" in r for r in result.reasons)


def test_failed_rollback_is_reported_not_hidden(make_guard, orders, scope):
    class BrokenRollback(DuckDBIcebergAdapter):
        def rollback(self, table, snapshot_id, expected_current=None):
            raise ExecutionError("catalog refused the rollback")

    guard = make_guard(adapter=BrokenRollback(orders), extra_invariants=[always_fails()])
    result = guard.submit(sql("DELETE FROM orders WHERE id = 1"), scope)
    assert result.outcome is Outcome.ROLLBACK_FAILED
    assert result.published and not result.rolled_back
    assert "catalog refused" in result.error


def test_pre_publish_failure_never_reaches_main_so_there_is_nothing_to_roll_back(
    make_guard, orders, scope
):
    guard = make_guard(extra_invariants=[always_fails(PRE_ONLY)])
    result = guard.submit(sql("DELETE FROM orders WHERE id = 1"), scope)
    assert not result.published and not result.rolled_back
    assert result.decision is Decision.ALLOW_VIA_BRANCH
    assert len(main_ids(orders)) == 1


# -- concurrent writers and failures while undoing a publish -------------------------------------


def racer_row() -> pa.Table:
    return pa.table(
        {"id": pa.array([999], pa.int64()), "customer": ["racer"], "amount": pa.array([1.0])}
    )


def test_a_writer_that_commits_before_the_rollback_is_never_discarded(make_guard, orders, scope):
    class RacyAdapter(DuckDBIcebergAdapter):
        def rollback(self, table, snapshot_id, expected_current=None):
            orders.table(table).append(racer_row())  # lands after the guard's own checks
            super().rollback(table, snapshot_id, expected_current)

    guard = make_guard(adapter=RacyAdapter(orders), extra_invariants=[always_fails()])
    result = guard.submit(sql("DELETE FROM orders WHERE id = 1"), scope)

    assert result.outcome is Outcome.ROLLBACK_BLOCKED and result.needs_attention
    assert result.published and not result.rolled_back
    assert 999 in order_ids(orders)
    assert any("post-publish check failed" in r for r in result.reasons)


def test_a_commit_that_lands_during_the_rollback_commit_is_not_discarded(
    make_guard, orders, scope, race_on_next_commit
):
    """The racing append is a real commit; PyIceberg's ref assertion must reject our rollback."""
    arm_after_publish = Invariant(
        "arm-race", lambda ctx: race_on_next_commit() or "fails on purpose", POST_ONLY
    )
    guard = make_guard(extra_invariants=[arm_after_publish])
    result = guard.submit(sql("DELETE FROM orders WHERE id = 1"), scope)

    assert result.outcome is Outcome.ROLLBACK_BLOCKED and result.needs_attention
    ids = order_ids(orders)
    assert 999 in ids and 1 not in ids  # the delete stays published, the other writer survives


def test_a_real_concurrent_commit_during_publish_refuses_the_publish(
    make_guard, orders, scope, race_on_next_commit
):
    class RacyPublish(DuckDBIcebergAdapter):
        def publish(self, branch, expected_head):
            race_on_next_commit()
            return super().publish(branch, expected_head)

    result = make_guard(adapter=RacyPublish(orders)).submit(
        sql("DELETE FROM orders WHERE id <= 2"), scope
    )
    assert result.outcome is Outcome.REFUSED_DIVERGED and not result.published
    ids = order_ids(orders)
    assert 999 in ids and 1 in ids and 2 in ids
    assert set(orders.refs("orders")) == {"main"}


def test_rollback_with_a_stale_expectation_is_refused_by_the_adapter(orders):
    from sqlguard.safety.executor import BranchDiverged

    adapter = DuckDBIcebergAdapter(orders)
    first = adapter.current_snapshot_id("orders")
    orders.table("orders").append(racer_row())
    with pytest.raises(BranchDiverged):
        adapter.rollback("orders", first, expected_current=first)
    assert 999 in order_ids(orders)


@pytest.mark.parametrize("error", [OSError("catalog database is locked"), RuntimeError("boom")])
def test_any_failure_while_rolling_back_is_reported_as_rollback_failed(
    make_guard, orders, scope, audit, error
):
    class Failing(DuckDBIcebergAdapter):
        def rollback(self, table, snapshot_id, expected_current=None):
            raise error

    guard = make_guard(adapter=Failing(orders), extra_invariants=[always_fails()])
    result = guard.submit(sql("DELETE FROM orders WHERE id = 1"), scope)

    assert result.outcome is Outcome.ROLLBACK_FAILED and result.needs_attention
    assert result.published and not result.rolled_back
    assert any("post-publish check failed" in r for r in result.reasons)  # reasons survive
    assert type(error).__name__ in result.error
    record = [r for r in audit.read() if r["phase"] == "completed"][-1]
    assert record["needs_attention"] is True and record["outcome"] == "rollback_failed"


def test_clean_outcomes_do_not_need_attention(make_guard, orders, scope, audit):
    guard = make_guard(extra_invariants=[always_fails()])
    rolled_back = guard.submit(sql("DELETE FROM orders WHERE id = 1"), scope)
    published = make_guard().submit(sql("DELETE FROM orders WHERE id = 2"), scope)
    assert not rolled_back.needs_attention and not published.needs_attention
    assert [r["needs_attention"] for r in audit.read() if r["phase"] == "completed"] == [
        False,
        False,
    ]
