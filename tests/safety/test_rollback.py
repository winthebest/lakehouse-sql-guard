"""A change that passes the pre-publish checks but fails verification on main is undone."""

from __future__ import annotations

import pyarrow as pa

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
        def rollback(self, table, snapshot_id):
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
