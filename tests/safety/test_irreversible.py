"""Destructive maintenance: needs an approver, passes live retention checks, and really is final."""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

import pyarrow as pa
import pytest

from sqlguard.core.findings import Decision
from sqlguard.safety import (
    DropTable,
    ExpireSnapshots,
    FastForward,
    Policy,
    RemoveOrphanFiles,
    RollbackToSnapshot,
    SqlAction,
)
from sqlguard.safety.executor import DuckDBIcebergAdapter, ExecutionError
from sqlguard.safety.executor.base import MaintenancePlan, PlannedSnapshot
from sqlguard.safety.guard import Outcome, _retention_violations
from sqlguard.safety.policy import RetentionRule
from tests.safety.conftest import ROWS, order_ids

REAL_NOW = datetime.now(UTC)
CLOCK_NOW = REAL_NOW + timedelta(days=60)  # snapshots written by the tests look two months old


def one_row(order_id: int) -> pa.Table:
    return pa.table(
        {"id": pa.array([order_id], pa.int64()), "customer": ["x"], "amount": pa.array([1.0])}
    )


@pytest.fixture
def history(orders):
    """`orders` with four snapshots; returns their ids oldest first."""
    for i in (1, 2, 3):
        orders.table("orders").append(one_row(1000 + i))
    return [s.snapshot_id for s in orders.table("orders").snapshots()]


@pytest.fixture
def guard(make_guard):
    return make_guard(clock=lambda: CLOCK_NOW)


def expire(days_before_clock: float = 10, **kwargs) -> ExpireSnapshots:
    return ExpireSnapshots(
        "orders", older_than=CLOCK_NOW - timedelta(days=days_before_clock), **kwargs
    )


def table_dir(orders) -> Path:
    return Path(urlparse(orders.table("orders").location()).path)


def parquet_files(orders) -> set[str]:
    return {p.name for p in table_dir(orders).rglob("*.parquet")}


def test_expire_is_irreversible_and_cannot_be_rolled_back(guard, orders, history, scope):
    first, *_, head = history
    approved_requests = []

    def approver(request):
        approved_requests.append(request)
        return True

    result = guard.submit(expire(), scope, approver)

    assert result.outcome is Outcome.MAINTENANCE_DONE and result.executed
    assert not result.published  # expiry never moves main
    assert [s.snapshot_id for s in orders.table("orders").snapshots()] == [head]
    # the approver was shown exactly what would be lost
    assert [s.snapshot_id for s in approved_requests[0].plan.expired] == history[:-1]

    with pytest.raises(ValueError, match="Cannot roll back to unknown snapshot id"):
        orders.table("orders").manage_snapshots().rollback_to_snapshot(first).commit()
    rollback = guard.submit(RollbackToSnapshot("orders", snapshot_id=first), scope, lambda r: True)
    assert rollback.outcome is Outcome.EXECUTION_FAILED
    assert "unknown snapshot id" in rollback.error
    assert len(order_ids(orders)) == ROWS + 3  # the current state is intact


def test_expire_without_an_approver_only_previews(guard, orders, history, scope):
    result = guard.submit(expire(), scope)
    assert result.decision is Decision.ASK and result.outcome is Outcome.AWAITING_APPROVAL
    assert not result.executed
    assert [s.snapshot_id for s in result.plan.expired] == history[:-1]
    assert result.plan.irreversible and result.plan.remaining_snapshots == 1
    assert len(orders.table("orders").snapshots()) == len(history)


def test_a_declining_approver_expires_nothing(guard, orders, history, scope):
    result = guard.submit(expire(), scope, lambda r: False)
    assert result.outcome is Outcome.REJECTED and not result.executed
    assert len(orders.table("orders").snapshots()) == len(history)


def test_expire_keeps_the_newest_retain_last_snapshots_and_all_ref_heads(
    guard, orders, history, scope
):
    iceberg = orders.table("orders")
    iceberg.manage_snapshots().create_tag(history[0], "keep-me").commit()
    result = guard.submit(expire(retain_last=2), scope, lambda r: True)
    remaining = [s.snapshot_id for s in orders.table("orders").snapshots()]
    assert result.outcome is Outcome.MAINTENANCE_DONE
    assert remaining == [history[0], history[2], history[3]]  # tag head + newest two


def test_retention_violation_is_a_hard_deny_even_with_an_approver(guard, orders, history, scope):
    called = []
    result = guard.submit(expire(days_before_clock=1), scope, lambda r: called.append(r) or True)
    assert result.decision is Decision.DENY and result.outcome is Outcome.DENIED
    assert not called and not result.executed
    assert len(orders.table("orders").snapshots()) == len(history)


def test_retention_is_checked_against_the_live_table(make_guard, orders, history, scope):
    """An engine that ignores retain_last must still be stopped by the live check."""

    class Overeager(DuckDBIcebergAdapter):
        def plan_maintenance(self, action):
            plan = super().plan_maintenance(action)
            everything = tuple(
                PlannedSnapshot(s.snapshot_id, s.timestamp_ms)
                for s in orders.table("orders").snapshots()
            )
            return MaintenancePlan(
                plan.kind,
                plan.table,
                True,
                everything,
                0,
                current_snapshot_id=plan.current_snapshot_id,
            )

    policy = Policy.from_dict({"retention": {"default": {"min_snapshots": 2}}})
    guard = make_guard(policy, adapter=Overeager(orders), clock=lambda: CLOCK_NOW)
    result = guard.submit(expire(retain_last=2), scope, lambda r: True)
    assert result.outcome is Outcome.DENIED and not result.executed
    assert any("minimum is 2" in r for r in result.reasons)
    assert len(orders.table("orders").snapshots()) == len(history)


def test_live_retention_checks_snapshot_and_file_ages():
    rule = RetentionRule(min_snapshot_age_days=7, min_snapshots=3, min_orphan_age_days=3)
    from sqlguard.safety.executor import OrphanFile

    now = datetime(2026, 11, 4, tzinfo=UTC)
    young_ms = int((now - timedelta(days=1)).timestamp() * 1000)
    old_ms = int((now - timedelta(days=30)).timestamp() * 1000)
    plan = MaintenancePlan(
        "expire_snapshots",
        "db.orders",
        True,
        expired=(PlannedSnapshot(1, old_ms), PlannedSnapshot(2, young_ms)),
        remaining_snapshots=2,
        orphan_files=(
            OrphanFile("file:///a", 1, now - timedelta(days=1)),
            OrphanFile("file:///b", 1, now - timedelta(days=9)),
        ),
    )
    problems = " | ".join(_retention_violations(plan, rule, now))
    assert "leave 2 snapshot(s); minimum is 3" in problems
    assert "snapshot 2 is 1.0 days old" in problems and "snapshot 1" not in problems
    assert "file:///a" in problems and "file:///b" not in problems
    assert _retention_violations(MaintenancePlan("rollback", "t", False), rule, now) == []


def test_snapshot_ids_and_missing_older_than_are_denied(guard, orders, history, scope):
    by_id = guard.submit(
        ExpireSnapshots("orders", snapshot_ids=(history[0],)), scope, lambda r: True
    )
    no_cutoff = guard.submit(ExpireSnapshots("orders"), scope, lambda r: True)
    assert by_id.outcome is Outcome.DENIED and no_cutoff.outcome is Outcome.DENIED
    assert len(orders.table("orders").snapshots()) == len(history)


def test_policy_can_deny_irreversible_actions_outright(make_guard, orders, history, scope):
    policy = Policy.from_dict({"class_decisions": {"irreversible": "deny"}})
    result = make_guard(policy, clock=lambda: CLOCK_NOW).submit(expire(), scope, lambda r: True)
    assert result.outcome is Outcome.DENIED and not result.executed


def stray_old_file(orders, name="data/stray.parquet") -> Path:
    path = table_dir(orders) / name
    path.write_bytes(b"orphan")
    old = time.time() - 30 * 86400
    os.utime(path, (old, old))
    return path


def test_remove_orphan_files_needs_approval_and_shows_a_dry_run_first(
    guard, orders, history, scope
):
    stray = stray_old_file(orders)
    cutoff = CLOCK_NOW - timedelta(days=5)
    action = RemoveOrphanFiles("orders", older_than=cutoff)

    preview = guard.submit(action, scope)
    assert preview.outcome is Outcome.AWAITING_APPROVAL and stray.exists()
    assert [Path(urlparse(f.location).path) for f in preview.plan.orphan_files] == [stray]

    seen = []
    done = guard.submit(action, scope, lambda r: seen.append(r.plan) or True)
    assert done.outcome is Outcome.MAINTENANCE_DONE and not stray.exists()
    assert seen[0].orphan_files == preview.plan.orphan_files
    assert len(order_ids(orders)) == ROWS + 3 and parquet_files(orders)


def test_orphan_cleanup_younger_than_the_retention_window_is_denied(guard, orders, scope):
    stray = stray_old_file(orders)
    result = guard.submit(
        RemoveOrphanFiles("orders", older_than=CLOCK_NOW - timedelta(days=1)), scope, lambda r: True
    )
    assert result.outcome is Outcome.DENIED and stray.exists()


def test_expire_plus_orphan_cleanup_is_what_destroys_data(guard, orders, scope):
    """Expiry alone leaves every Parquet file; the follow-up cleanup deletes the old data."""
    first = orders.table("orders").current_snapshot().snapshot_id
    orders.table("orders").overwrite(one_row(501))
    orders.table("orders").overwrite(one_row(502))
    files_before = parquet_files(orders)
    approve = lambda r: True  # noqa: E731

    guard.submit(expire(), scope, approve)
    assert parquet_files(orders) == files_before  # still on disk after expiry

    cleanup = guard.submit(
        RemoveOrphanFiles("orders", older_than=CLOCK_NOW - timedelta(days=5)), scope, approve
    )
    assert cleanup.outcome is Outcome.MAINTENANCE_DONE
    assert len(parquet_files(orders)) < len(files_before)
    assert order_ids(orders) == [502]  # the current state survives
    with pytest.raises(Exception):  # noqa: B017, PT011  # the original data is gone for good
        orders.read("orders", first)


def test_spark_call_text_takes_the_same_path(guard, orders, history, scope):
    cutoff = (CLOCK_NOW - timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S")
    call = SqlAction(
        f"CALL system.expire_snapshots(table => 'orders', older_than => TIMESTAMP '{cutoff}')"
    )
    waiting = guard.submit(call, scope)
    assert waiting.outcome is Outcome.AWAITING_APPROVAL
    done = guard.submit(call, scope, lambda r: True)
    assert done.outcome is Outcome.MAINTENANCE_DONE
    assert len(orders.table("orders").snapshots()) == 1


def test_rollback_to_snapshot_moves_main_back_after_approval(guard, orders, history, scope):
    first = history[0]
    count = len(orders.table("orders").snapshots())
    action = RollbackToSnapshot("orders", snapshot_id=first)

    assert guard.submit(action, scope).outcome is Outcome.AWAITING_APPROVAL
    assert len(order_ids(orders)) == ROWS + 3

    result = guard.submit(action, scope, lambda r: True)
    assert result.outcome is Outcome.MAINTENANCE_DONE and result.published
    assert order_ids(orders) == list(range(1, ROWS + 1))
    assert len(orders.table("orders").snapshots()) == count  # reversible: nothing was deleted
    assert result.snapshots_after["db.orders"] == first


def test_rollback_to_a_snapshot_outside_main_history_is_refused(guard, orders, history, scope):
    adapter = DuckDBIcebergAdapter(orders)
    branch = adapter.create_branch("orders")
    adapter.apply_sql(branch, "DELETE FROM orders WHERE id = 1")
    stray_head = orders.table("orders").refs()[branch.name].snapshot_id
    result = guard.submit(
        RollbackToSnapshot("orders", snapshot_id=stray_head), scope, lambda r: True
    )
    assert result.outcome is Outcome.EXECUTION_FAILED and "not an ancestor" in result.error


def test_fast_forward_publishes_a_branch_and_refuses_diverged_ones(guard, orders, scope):
    adapter = DuckDBIcebergAdapter(orders)
    branch = adapter.create_branch("orders", "feature")
    adapter.apply_sql(branch, "DELETE FROM orders WHERE id = 1")
    head = orders.table("orders").refs()["feature"].snapshot_id

    other = adapter.create_branch("orders", "other")
    adapter.apply_sql(other, "DELETE FROM orders WHERE id = 2")

    action = FastForward("orders", branch="main", to="feature")
    assert guard.submit(action, scope).outcome is Outcome.AWAITING_APPROVAL
    done = guard.submit(action, scope, lambda r: True)
    assert done.outcome is Outcome.MAINTENANCE_DONE
    assert orders.table("orders").current_snapshot().snapshot_id == head

    stale = guard.submit(FastForward("orders", branch="main", to="other"), scope, lambda r: True)
    assert stale.outcome is Outcome.EXECUTION_FAILED and "cannot fast-forward" in stale.error
    assert orders.table("orders").current_snapshot().snapshot_id == head


def test_drop_table_is_never_executed_by_this_adapter(guard, orders, scope):
    result = guard.submit(DropTable("orders", purge=True), scope, lambda r: True)
    assert result.outcome is Outcome.UNSUPPORTED and not result.executed
    assert orders.has_table("orders") and len(order_ids(orders)) == ROWS


def test_maintenance_on_a_missing_table_fails_cleanly(guard, scope):
    action = ExpireSnapshots("ghost", older_than=CLOCK_NOW - timedelta(days=10))
    result = guard.submit(action, scope, lambda r: True)
    assert result.outcome is Outcome.EXECUTION_FAILED and not result.executed
    with pytest.raises(ExecutionError):
        DuckDBIcebergAdapter(guard.catalog).plan_maintenance(
            RollbackToSnapshot("ghost", snapshot_id=1)
        )


def test_maintenance_is_audited_with_plan_and_result(guard, orders, history, scope, audit):
    guard.submit(expire(), scope)
    guard.submit(expire(), scope, lambda r: True)
    waiting, done = audit.read()
    assert waiting["outcome"] == "awaiting_approval" and waiting["approval"] == "missing"
    assert waiting["maintenance_plan"]["expired_snapshot_ids"] == history[:-1]
    assert done["approval"] == "approved" and done["executed"] is True
    assert done["maintenance_result"]["expired_snapshot_ids"] == history[:-1]
    assert done["class"] == "irreversible"
