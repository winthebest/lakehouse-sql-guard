from __future__ import annotations

import pyarrow as pa
import pyarrow.compute as pc
import pytest

from sqlguard.core.catalog import LakehouseCatalog
from sqlguard.core.findings import Decision
from sqlguard.safety import Policy, PolicyError, SqlAction
from sqlguard.safety.audit import AuditError, AuditLog
from sqlguard.safety.executor import BranchDiverged, DuckDBIcebergAdapter
from sqlguard.safety.guard import ApprovalRequest, Guard, Outcome
from tests.safety.conftest import ROWS, main_ids, order_ids


def sql(text: str) -> SqlAction:
    return SqlAction(text)


def test_read_returns_rows_without_touching_anything(guard, orders, scope):
    result = guard.submit(sql("SELECT count(*) AS n FROM orders"), scope)
    assert result.outcome is Outcome.READ and result.decision is Decision.ALLOW
    assert result.data.column("n").to_pylist() == [ROWS]
    assert result.executed and not result.published
    assert len(main_ids(orders)) == 1 and set(orders.refs("orders")) == {"main"}


def test_small_delete_is_rehearsed_then_published(guard, orders, scope):
    result = guard.submit(sql("DELETE FROM orders WHERE id <= 2"), scope)
    assert result.outcome is Outcome.PUBLISHED and result.decision is Decision.ALLOW_VIA_BRANCH
    assert result.executed and result.published and not result.rolled_back
    table = result.impact.tables[0]
    assert (table.rows_before, table.rows_after, table.rows_deleted) == (ROWS, ROWS - 2, 2)
    assert order_ids(orders) == list(range(3, ROWS + 1))
    assert set(orders.refs("orders")) == {"main"}  # audit branch cleaned up
    assert result.snapshots_before != result.snapshots_after


def test_insert_and_update_are_published(guard, orders, scope):
    inserted = guard.submit(sql("INSERT INTO orders VALUES (100, 'new', 5.0)"), scope)
    assert inserted.outcome is Outcome.PUBLISHED
    assert inserted.impact.tables[0].rows_added == 1
    updated = guard.submit(sql("UPDATE orders SET amount = amount + 1 WHERE id = 100"), scope)
    assert updated.outcome is Outcome.PUBLISHED
    assert updated.impact.tables[0].rows_deleted == 1  # keyless: the old version of the row
    amounts = orders.read("orders").filter(pc.equal(orders.read("orders")["id"], 100))
    assert amounts.column("amount").to_pylist() == [6.0]


def test_statement_that_changes_nothing_publishes_nothing(guard, orders, scope):
    before = main_ids(orders)
    result = guard.submit(sql("UPDATE orders SET amount = 0 WHERE id = 9999"), scope)
    assert result.outcome is Outcome.NO_CHANGE and not result.published
    assert main_ids(orders) == before


def test_wrong_delete_is_caught_by_the_dry_run_and_needs_approval(guard, orders, scope):
    """`id IS NOT NULL` looks selective, so only the rehearsal reveals it deletes everything."""
    before = main_ids(orders)
    result = guard.submit(sql("DELETE FROM orders WHERE id IS NOT NULL"), scope)
    assert result.decision is Decision.ASK
    assert result.outcome is Outcome.AWAITING_APPROVAL
    assert result.executed and not result.published
    assert result.impact.tables[0].affected_pct == 100.0
    assert any("100.0% of rows" in r for r in result.reasons)
    assert main_ids(orders) == before and order_ids(orders) == list(range(1, ROWS + 1))
    assert set(orders.refs("orders")) == {"main"}


def test_approver_sees_the_measured_impact_and_can_approve_or_reject(make_guard, orders, scope):
    seen: list[ApprovalRequest] = []

    def reject(request):
        seen.append(request)
        return False

    guard = make_guard()
    rejected = guard.submit(sql("DELETE FROM orders WHERE id IS NOT NULL"), scope, reject)
    assert rejected.outcome is Outcome.REJECTED and not rejected.published
    assert seen[0].impact.tables[0].rows_deleted == ROWS
    assert order_ids(orders) == list(range(1, ROWS + 1))

    approved = guard.submit(sql("DELETE FROM orders WHERE id IS NOT NULL"), scope, lambda r: True)
    assert approved.outcome is Outcome.PUBLISHED and approved.published
    assert order_ids(orders) == []


def test_crashing_approver_counts_as_rejection(guard, orders, scope):
    def broken(request):
        raise RuntimeError("boom")

    result = guard.submit(sql("DELETE FROM orders"), scope, broken)
    assert result.outcome is Outcome.REJECTED and "RuntimeError" in result.error
    assert order_ids(orders) == list(range(1, ROWS + 1))


def test_statically_risky_write_without_approver_never_executes(guard, orders, scope):
    result = guard.submit(sql("DELETE FROM orders"), scope)
    assert result.decision is Decision.ASK and result.outcome is Outcome.AWAITING_APPROVAL
    assert not result.executed and result.impact is None
    assert set(orders.refs("orders")) == {"main"} and len(orders.table("orders").snapshots()) == 1


def test_statically_risky_write_with_approver_is_still_rehearsed_on_a_branch(guard, orders, scope):
    result = guard.submit(sql("DELETE FROM orders"), scope, lambda r: r.impact is not None)
    assert result.outcome is Outcome.PUBLISHED
    assert result.impact.tables[0].rows_deleted == ROWS


@pytest.mark.parametrize(
    "statement",
    [
        "DROP TABLE orders",
        "SELECT * FROM other.secrets",
        "COPY orders TO 'out.csv'",
        "DELETE FROM warehouse.orders WHERE id = 1",
        "this is not sql",
    ],
)
def test_denied_actions_never_run(guard, orders, scope, statement):
    before = main_ids(orders)
    approver_called = []
    result = guard.submit(sql(statement), scope, lambda r: approver_called.append(r) or True)
    assert result.decision in (Decision.DENY, Decision.ASK)
    if result.decision is Decision.DENY:
        assert result.outcome is Outcome.DENIED and not approver_called
    assert not result.published and main_ids(orders) == before


def test_out_of_scope_table_is_denied(guard, orders):
    result = guard.submit(sql("DELETE FROM orders WHERE id = 1"), None)
    assert result.outcome is Outcome.DENIED and not result.executed
    assert order_ids(orders) == list(range(1, ROWS + 1))


def test_pre_publish_invariant_blocks_the_publish(make_guard, orders, scope):
    policy = Policy.from_dict({"invariants": {"default": {"max_delete_pct": 5}}})
    result = make_guard(policy).submit(sql("DELETE FROM orders WHERE id <= 4"), scope)
    assert result.outcome is Outcome.INVARIANT_FAILED and not result.published
    assert any("max_delete_pct" in r for r in result.reasons)
    assert [r.passed for r in result.invariants] == [False]
    assert order_ids(orders) == list(range(1, ROWS + 1)) and set(orders.refs("orders")) == {"main"}


def test_row_count_bounds_not_null_and_unique_key(make_guard, orders, scope):
    policy = Policy.from_dict(
        {
            "invariants": {
                "default": {"min_rows": 15, "not_null": ["customer"], "unique_key": ["id"]}
            }
        }
    )
    guard = make_guard(policy)
    shrink = guard.submit(sql("DELETE FROM orders WHERE id <= 6"), scope)
    assert shrink.outcome is Outcome.INVARIANT_FAILED
    assert "min_rows" in " ".join(shrink.reasons)

    null_customer = guard.submit(sql("INSERT INTO orders VALUES (50, NULL, 1.0)"), scope)
    assert null_customer.outcome is Outcome.INVARIANT_FAILED
    assert "not_null" in " ".join(null_customer.reasons)

    duplicate = guard.submit(sql("INSERT INTO orders VALUES (1, 'dup', 1.0)"), scope)
    assert duplicate.outcome is Outcome.INVARIANT_FAILED
    assert "unique_key" in " ".join(duplicate.reasons)

    fine = guard.submit(sql("INSERT INTO orders VALUES (51, 'ok', 1.0)"), scope)
    assert fine.outcome is Outcome.PUBLISHED
    assert all(r.passed for r in fine.invariants)
    assert order_ids(orders)[-1] == 51 and len(order_ids(orders)) == ROWS + 1


def test_unique_key_makes_the_impact_report_count_modified_rows(make_guard, scope):
    policy = Policy.from_dict({"invariants": {"default": {"unique_key": ["id"]}}})
    result = make_guard(policy).submit(sql("UPDATE orders SET amount = 99 WHERE id <= 2"), scope)
    table = result.impact.tables[0]
    assert (table.rows_modified, table.rows_deleted, table.rows_added) == (2, 0, 0)
    assert table.rows_affected == 2 and table.key == ("id",)


def test_engine_error_is_reported_and_leaves_main_alone(guard, orders, scope):
    before = main_ids(orders)
    result = guard.submit(sql("UPDATE orders SET nope = 1 WHERE id = 1"), scope)
    assert result.outcome is Outcome.EXECUTION_FAILED and result.error
    assert main_ids(orders) == before and set(orders.refs("orders")) == {"main"}


def test_unsupported_shapes_are_not_executed(guard, orders, scope):
    approve = lambda r: True  # noqa: E731
    ddl = guard.submit(sql("ALTER TABLE orders ADD COLUMN note VARCHAR"), scope, approve)
    assert ddl.outcome is Outcome.UNSUPPORTED and not ddl.executed
    orders.create_table("archive", pa.table({"id": pa.array([1], pa.int64())}))
    multi = guard.submit(
        sql("DELETE FROM orders WHERE id = 1; DELETE FROM archive WHERE id = 1"), scope
    )
    assert multi.outcome is Outcome.UNSUPPORTED and not multi.executed
    assert order_ids(orders) == list(range(1, ROWS + 1))


def test_missing_table_is_reported(guard, scope):
    result = guard.submit(sql("DELETE FROM ghost WHERE id = 1"), scope)
    assert result.outcome is Outcome.EXECUTION_FAILED and "does not exist" in " ".join(
        result.reasons
    )


class DivergingAdapter(DuckDBIcebergAdapter):
    """Lets someone else commit to main while the guard is still checking its branch."""

    def diff(self, branch, key=()):
        result = super().diff(branch, key)
        self.catalog.table("orders").append(
            pa.table(
                {
                    "id": pa.array([777], pa.int64()),
                    "customer": ["other"],
                    "amount": pa.array([1.0]),
                }
            )
        )
        return result


def test_diverged_main_refuses_the_publish(make_guard, orders, scope):
    guard = make_guard(adapter=DivergingAdapter(orders))
    result = guard.submit(sql("DELETE FROM orders WHERE id <= 2"), scope)
    assert result.outcome is Outcome.REFUSED_DIVERGED
    assert result.decision is Decision.ASK and not result.published
    assert "moved" in " ".join(result.reasons)
    # the other writer's row survives and our delete never landed
    ids = order_ids(orders)
    assert 777 in ids and 1 in ids and 2 in ids
    assert set(orders.refs("orders")) == {"main"}


def test_adapter_publish_raises_on_divergence(orders):
    adapter = DuckDBIcebergAdapter(orders)
    branch = adapter.create_branch("orders")
    adapter.apply_sql(branch, "DELETE FROM orders WHERE id = 1")
    orders.table("orders").append(
        pa.table({"id": pa.array([5000], pa.int64()), "customer": ["x"], "amount": pa.array([1.0])})
    )
    with pytest.raises(BranchDiverged):
        adapter.publish(branch, branch.head)


class SpyAdapter(DuckDBIcebergAdapter):
    """Records main's snapshot id after every call so tests can see when main moved."""

    def __init__(self, catalog: LakehouseCatalog) -> None:
        super().__init__(catalog)
        self.trace: list[tuple[str, int | None]] = []

    def __getattribute__(self, name):
        attr = super().__getattribute__(name)
        if name in {"create_branch", "apply_sql", "diff", "publish", "drop_branch", "rollback"}:

            def traced(*args, **kwargs):
                try:
                    return attr(*args, **kwargs)
                finally:
                    main = self.catalog.table("orders").current_snapshot()
                    self.trace.append((name, main.snapshot_id if main else None))

            return traced
        return attr


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO orders VALUES (200, 'x', 1.0)",
        "DELETE FROM orders WHERE id = 3",
        "UPDATE orders SET amount = 0 WHERE id = 4",
        "DELETE FROM orders WHERE id IS NOT NULL",
    ],
)
def test_main_only_moves_when_the_branch_is_published(make_guard, orders, scope, statement):
    adapter = SpyAdapter(orders)
    start = orders.table("orders").current_snapshot().snapshot_id
    result = make_guard(adapter=adapter).submit(sql(statement), scope)

    names = [name for name, _ in adapter.trace]
    first_publish = names.index("publish") if "publish" in names else len(names)
    for name, main in adapter.trace[:first_publish]:
        assert main == start, f"main moved during {name}, before any publish"
    if result.published:
        assert names.count("publish") == 1
        assert adapter.trace[first_publish][1] != start
    else:
        assert orders.table("orders").current_snapshot().snapshot_id == start


def test_every_submission_is_audited_with_all_fields(make_guard, orders, scope, audit):
    guard = make_guard()
    results = [
        guard.submit(sql("SELECT 1"), scope),
        guard.submit(sql("DELETE FROM orders WHERE id = 1"), scope),
        guard.submit(sql("DELETE FROM orders"), scope),
        guard.submit(sql("DROP TABLE orders PURGE"), scope),
        guard.submit(sql("SELECT * FROM nowhere.t"), scope),
        guard.submit(sql("DELETE FROM orders WHERE id IS NOT NULL"), scope),
    ]
    records = audit.read()
    completed = [r for r in records if r["phase"] == "completed"]
    assert [r["id"] for r in completed] == [r.audit_id for r in results]
    required = {
        "id",
        "ts",
        "phase",
        "action",
        "classification",
        "class",
        "decision",
        "reasons",
        "approval",
        "approver",
        "outcome",
        "needs_attention",
        "executed",
        "published",
        "rolled_back",
        "impact",
        "invariants",
        "snapshots_before",
        "snapshots_after",
        "error",
    }
    for record, result in zip(completed, results, strict=True):
        assert required <= set(record)
        assert record["outcome"] == result.outcome.value
        assert record["decision"] == result.decision.value
    published = completed[1]
    assert published["snapshots_before"]["db.orders"] != published["snapshots_after"]["db.orders"]
    assert published["impact"]["tables"][0]["rows_deleted"] == 1
    assert completed[0]["action"]["sql"] == "SELECT ?" and completed[0]["action"]["redacted"]

    # side-effecting submissions leave a "started" record before the completion record
    started_ids = [r["id"] for r in records if r["phase"] == "started"]
    assert started_ids == [results[1].audit_id, results[5].audit_id]
    first_started = next(i for i, r in enumerate(records) if r["phase"] == "started")
    assert records[first_started]["id"] == results[1].audit_id
    assert records[first_started]["snapshots_before"]["db.orders"] is not None
    assert records[first_started]["outcome"] is None


def test_audit_log_is_append_only_across_guards(make_guard, scope, audit):
    make_guard().submit(sql("SELECT 1"), scope)
    first = audit.path.read_text()
    make_guard().submit(sql("SELECT 2"), scope)
    second = audit.path.read_text()
    assert second.startswith(first) and len(second.splitlines()) == 2


def test_unwritable_audit_log_blocks_everything(orders, scope, tmp_path):
    directory = tmp_path / "is_a_dir"
    directory.mkdir()
    guard = Guard(orders, audit_path=directory)
    with pytest.raises(AuditError):
        guard.submit(sql("DELETE FROM orders WHERE id = 1"), scope)
    assert order_ids(orders) == list(range(1, ROWS + 1))


def test_result_is_json_serialisable(guard, scope):
    import json

    result = guard.submit(sql("DELETE FROM orders WHERE id = 1"), scope)
    payload = json.loads(json.dumps(result.to_dict()))
    assert payload["outcome"] == "published" and payload["published"] is True


@pytest.mark.parametrize(
    "bad",
    [
        {"invariants": {"nope": 1}},
        {"invariants": {"default": {"max_delete_pct": -1}}},
        {"invariants": {"default": {"max_delete_pct": 150}}},
        {"invariants": {"default": {"unique_key": "id"}}},
        {"invariants": {"default": {"bogus": 1}}},
    ],
)
def test_bad_invariant_config_is_rejected(bad):
    with pytest.raises(PolicyError):
        Policy.from_dict(bad)


def test_table_invariants_override_the_default_field_by_field():
    policy = Policy.from_dict(
        {
            "invariants": {
                "default": {"max_delete_pct": 50, "min_rows": 1},
                "tables": {"orders": {"max_delete_pct": 10, "not_null": ["id"]}},
            }
        }
    )
    merged = policy.invariants.for_table("db.orders")
    assert (merged.max_delete_pct, merged.min_rows, merged.not_null) == (10.0, 1, ("id",))
    assert policy.invariants.for_table("db.other").max_delete_pct == 50.0


# -- approval must be exactly True ---------------------------------------------------------------


@pytest.mark.parametrize("answer", ["yes", "n", 1, [1], {"ok": True}, object(), None])
def test_only_a_literal_true_approves(guard, orders, scope, audit, answer):
    result = guard.submit(sql("DELETE FROM orders WHERE id IS NOT NULL"), scope, lambda r: answer)
    assert result.outcome is Outcome.REJECTED and not result.published
    assert order_ids(orders) == list(range(1, ROWS + 1))
    assert audit.read()[-1]["approval"] == "invalid_response"


def test_a_literal_false_is_a_plain_rejection(guard, scope, audit):
    guard.submit(sql("DELETE FROM orders WHERE id IS NOT NULL"), scope, lambda r: False)
    assert audit.read()[-1]["approval"] == "rejected"


def test_truthy_answers_cannot_approve_irreversible_maintenance(make_guard, orders, scope):
    from datetime import UTC, datetime, timedelta

    from sqlguard.safety import ExpireSnapshots

    now = datetime.now(UTC) + timedelta(days=60)
    orders.table("orders").append(
        pa.table({"id": pa.array([1], pa.int64()), "customer": ["x"], "amount": pa.array([1.0])})
    )
    guard = make_guard(clock=lambda: now)
    action = ExpireSnapshots("orders", older_than=now - timedelta(days=10))
    result = guard.submit(action, scope, lambda r: "approved")
    assert result.outcome is Outcome.REJECTED and not result.executed
    assert len(orders.table("orders").snapshots()) == 2


def test_approver_identity_is_recorded_when_provided(guard, scope, audit):
    def approve(request):
        return True

    approve.approver_id = "alice@example.com"
    guard.submit(sql("DELETE FROM orders WHERE id IS NOT NULL"), scope, approve)
    guard.submit(sql("DELETE FROM orders"), scope, lambda r: False)
    records = [r for r in audit.read() if r["phase"] == "completed"]
    assert records[0]["approver"] == "alice@example.com" and records[1]["approver"] is None


# -- the audit trail survives a failing log --------------------------------------------------------


class FlakyAudit(AuditLog):
    def __init__(self, path, fail_on: str) -> None:
        super().__init__(path)
        self.fail_on = fail_on

    def append(self, record):
        if record.get("phase") == self.fail_on:
            raise AuditError("No space left on device")
        return super().append(record)


def test_failed_completion_record_still_returns_the_result_and_keeps_the_intent(
    orders, scope, audit_path, caplog
):
    guard = Guard(orders, audit_path=FlakyAudit(audit_path, "completed"))
    with caplog.at_level("CRITICAL", logger="sqlguard.audit"):
        result = guard.submit(sql("DELETE FROM orders WHERE id = 1"), scope)

    assert result.outcome is Outcome.PUBLISHED and result.published
    assert "No space left" in result.audit_error
    assert 1 not in order_ids(orders)  # the change is live ...
    records = AuditLog(audit_path).read()  # ... and the log says what was attempted
    assert [r["phase"] for r in records] == ["started"]
    assert records[0]["id"] == result.audit_id
    assert records[0]["action"]["sql_sha256"] and records[0]["classification"]["write_tables"]
    assert records[0]["snapshots_before"]["db.orders"] is not None
    assert "audit completion record could not be written" in caplog.text
    assert result.audit_id in caplog.text  # the full record went to the fallback log


def test_failed_intent_record_stops_the_action_before_it_runs(orders, scope, audit_path):
    guard = Guard(orders, audit_path=FlakyAudit(audit_path, "started"))
    with pytest.raises(AuditError):
        guard.submit(sql("DELETE FROM orders WHERE id = 1"), scope)
    assert order_ids(orders) == list(range(1, ROWS + 1)) and set(orders.refs("orders")) == {"main"}


def test_interruption_during_publish_is_audited_and_cleaned_up(orders, scope, audit_path):
    class Interrupted(DuckDBIcebergAdapter):
        def publish(self, branch, expected_head):
            raise KeyboardInterrupt

    guard = Guard(orders, audit_path=audit_path, adapter=Interrupted(orders))
    with pytest.raises(KeyboardInterrupt):
        guard.submit(sql("DELETE FROM orders WHERE id = 1"), scope)
    completed = [r for r in AuditLog(audit_path).read() if r["phase"] == "completed"]
    assert completed[0]["outcome"] == "error" and "KeyboardInterrupt" in completed[0]["error"]
    assert order_ids(orders) == list(range(1, ROWS + 1)) and set(orders.refs("orders")) == {"main"}


# -- audit content: redaction and bounded text -----------------------------------------------------


def test_sql_literals_are_redacted_in_the_audit_but_hashed(guard, scope, audit):
    import hashlib

    text = "INSERT INTO orders VALUES (77, 'secret-token-123', 4.5)"
    guard.submit(sql(text), scope)
    action = [r for r in audit.read() if r["phase"] == "completed"][-1]["action"]
    assert action["sql"] == "INSERT INTO orders VALUES (?, ?, ?)"
    assert action["sql_sha256"] == hashlib.sha256(text.encode()).hexdigest()
    assert "secret-token-123" not in audit.path.read_text()


def test_redaction_can_be_switched_off(make_guard, scope, audit):
    make_guard(redact_sql_literals=False).submit(sql("SELECT 'visible'"), scope)
    action = audit.read()[-1]["action"]
    assert action["sql"] == "SELECT 'visible'" and action["redacted"] is False


def test_unparseable_sql_is_not_stored_verbatim(guard, scope, audit):
    guard.submit(sql("DELETE FROM orders WHERE name = 'pii-value' garbage garbage ((("), scope)
    assert "pii-value" not in audit.path.read_text()
    assert audit.read()[-1]["action"]["sql_sha256"]


def test_engine_error_text_is_truncated(guard, orders, scope, audit):
    value = "x" * 1000
    result = guard.submit(sql(f"UPDATE orders SET id = '{value}' WHERE id = 1"), scope)
    assert result.outcome is Outcome.EXECUTION_FAILED
    assert len(result.error) <= 300 and all(len(r) <= 300 for r in result.reasons)
    record = audit.read()[-1]
    assert len(record["error"]) <= 300 and all(len(r) <= 300 for r in record["reasons"])
    assert value not in audit.path.read_text()
