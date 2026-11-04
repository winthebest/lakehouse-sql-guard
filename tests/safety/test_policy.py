from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from sqlguard.core.findings import Decision
from sqlguard.safety import (
    Classification,
    ExpireSnapshots,
    Impact,
    Policy,
    PolicyError,
    RemoveOrphanFiles,
    ReversibilityClass,
    SqlAction,
    TaskScope,
    classify,
)

C = ReversibilityClass
NOW = datetime(2026, 11, 4, tzinfo=UTC)
EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "policy.example.yaml"
SCOPE = TaskScope.of(namespaces=["db"])


def decide(sql: str, policy: Policy | None = None, scope: TaskScope | None = SCOPE):
    return (policy or Policy()).decide(classify(SqlAction(sql)), scope, now=NOW)


def test_default_decisions_per_class():
    assert decide("SELECT * FROM orders").decision is Decision.ALLOW
    assert decide("DELETE FROM orders WHERE id = 1").decision is Decision.ALLOW_VIA_BRANCH
    assert decide("DELETE FROM orders").decision is Decision.ASK
    assert decide("ALTER TABLE orders DROP COLUMN a").decision is Decision.ASK
    assert decide("DROP TABLE orders PURGE").decision is Decision.ASK
    assert decide("SELECT * FROM other.orders").decision is Decision.DENY
    assert decide("COMMIT").decision is Decision.DENY


def test_no_scope_anywhere_means_no_table_is_allowed():
    denied = decide("SELECT * FROM orders", scope=None)
    assert denied.klass is C.OUT_OF_SCOPE and denied.decision is Decision.DENY
    assert decide("SELECT 1", scope=None).decision is Decision.ALLOW


def test_policy_scope_is_used_when_the_caller_passes_none():
    policy = Policy(task_scope=TaskScope.of(["orders"]))
    assert decide("SELECT * FROM orders", policy, scope=None).decision is Decision.ALLOW
    assert decide("SELECT * FROM order_items", policy, scope=None).decision is Decision.DENY


def test_every_non_allow_decision_has_a_reason():
    for sql in ["DELETE FROM orders", "SELECT * FROM x.y", "COMMIT", "DROP TABLE orders"]:
        result = decide(sql)
        assert result.decision is not Decision.ALLOW and result.reasons


def test_multi_statement_reasons_are_numbered_and_strictest_wins():
    result = decide("SELECT 1; DELETE FROM orders WHERE id = 1; DROP TABLE orders")
    assert result.decision is Decision.ASK
    assert [s.decision for s in result.statements] == [
        Decision.ALLOW,
        Decision.ALLOW_VIA_BRANCH,
        Decision.ASK,
    ]
    assert all(r.startswith("[statement ") for r in result.reasons)


def test_protected_tables_block_writes_but_not_reads():
    policy = Policy(protected_tables=frozenset({"db.customers"}))
    assert decide("SELECT * FROM customers", policy).decision is Decision.ALLOW
    blocked = decide("DELETE FROM customers WHERE id = 1", policy)
    assert blocked.klass is C.OUT_OF_SCOPE and blocked.decision is Decision.DENY
    assert any("protected" in r for r in blocked.reasons)


def test_scope_compares_whole_table_names():
    scope = TaskScope.of(tables=["orders"])
    assert decide("SELECT * FROM orders", scope=scope).decision is Decision.ALLOW
    assert decide("SELECT * FROM orders_archive", scope=scope).decision is Decision.DENY
    assert decide("SELECT * FROM other.orders", scope=scope).decision is Decision.DENY


def test_impact_threshold_escalates_row_modifying_writes_only():
    policy = Policy()
    delete = classify(SqlAction("DELETE FROM orders WHERE id > 0"))
    small = policy.decide(delete.with_impact(Impact(5, 100)), SCOPE, now=NOW)
    large = policy.decide(delete.with_impact(Impact(50, 100)), SCOPE, now=NOW)
    assert small.decision is Decision.ALLOW_VIA_BRANCH
    assert large.decision is Decision.ASK and large.klass is C.RISKY_WRITE
    assert any("50.0%" in r for r in large.reasons)

    insert = classify(SqlAction("INSERT INTO orders SELECT * FROM order_items"))
    heavy_insert = policy.decide(insert.with_impact(Impact(1000, 10)), SCOPE, now=NOW)
    assert heavy_insert.decision is Decision.ALLOW_VIA_BRANCH


def test_absolute_row_threshold_and_unknown_impact():
    policy = Policy.from_dict(
        {"thresholds": {"max_affected_row_pct": None, "max_affected_rows": 10}}
    )
    delete = classify(SqlAction("DELETE FROM orders WHERE id > 0"))
    over = policy.decide(delete.with_impact(Impact(rows_affected=11)), SCOPE, now=NOW)
    assert over.decision is Decision.ASK
    unmeasured = policy.decide(delete.with_impact(Impact()), SCOPE, now=NOW)
    assert unmeasured.decision is Decision.ALLOW_VIA_BRANCH


def retention_decision(action, policy: Policy | None = None):
    return (policy or Policy()).decide(classify(action), SCOPE, now=NOW)


def test_expire_snapshots_retention():
    ok = retention_decision(ExpireSnapshots("orders", NOW - timedelta(days=30), retain_last=3))
    assert ok.decision is Decision.ASK

    for action in [
        ExpireSnapshots("orders", NOW - timedelta(days=1), retain_last=3),
        ExpireSnapshots("orders", None, retain_last=3),
        ExpireSnapshots("orders", NOW + timedelta(days=1), retain_last=3),
        ExpireSnapshots("orders", NOW - timedelta(days=30), snapshot_ids=(1,)),
    ]:
        denied = retention_decision(action)
        assert denied.decision is Decision.DENY
        assert denied.klass is C.IRREVERSIBLE
        assert any(r.startswith("retention violation") for r in denied.reasons)


def test_per_table_retention_overrides_default():
    policy = Policy.from_dict(
        {"retention": {"tables": {"orders": {"min_snapshot_age_days": 30, "min_snapshots": 5}}}}
    )
    young = ExpireSnapshots("orders", NOW - timedelta(days=20), retain_last=5)
    few = ExpireSnapshots("orders", NOW - timedelta(days=40), retain_last=2)
    other = ExpireSnapshots("order_items", NOW - timedelta(days=20), retain_last=1)
    assert retention_decision(young, policy).decision is Decision.DENY
    assert retention_decision(few, policy).decision is Decision.DENY
    assert retention_decision(other, policy).decision is Decision.ASK


def test_naive_timestamps_are_read_as_utc():
    naive = (NOW - timedelta(days=30)).replace(tzinfo=None)
    assert retention_decision(ExpireSnapshots("orders", naive, 1)).decision is Decision.ASK


def test_orphan_file_retention():
    assert retention_decision(RemoveOrphanFiles("orders", NOW - timedelta(days=5))).decision is (
        Decision.ASK
    )
    assert retention_decision(RemoveOrphanFiles("orders", NOW - timedelta(hours=1))).decision is (
        Decision.DENY
    )


def test_out_of_scope_table_is_reported_before_retention():
    result = retention_decision(ExpireSnapshots("other.t", NOW - timedelta(days=1)))
    assert result.klass is C.OUT_OF_SCOPE and result.decision is Decision.DENY


def test_example_policy_loads():
    policy = Policy.load(EXAMPLE)
    assert policy.default_namespace == "db"
    assert policy.class_decisions[C.REVERSIBLE_WRITE] is Decision.ALLOW_VIA_BRANCH
    assert policy.protected_tables == frozenset({"db.customers_pii"})
    assert policy.retention_for("db.orders").min_snapshots == 5
    assert policy.retention_for("db.other").min_snapshot_age_days == 7
    assert policy.task_scope is not None and policy.task_scope.allows("db.anything")
    assert policy.thresholds.max_affected_row_pct == 10


def test_empty_policy_file_gives_strict_defaults(tmp_path: Path):
    path = tmp_path / "policy.yaml"
    path.write_text("")
    assert Policy.load(path).class_decisions == Policy().class_decisions


def test_class_decisions_can_be_relaxed_within_limits():
    policy = Policy.from_dict({"class_decisions": {"risky_write": "allow_via_branch"}})
    assert decide("DELETE FROM orders", policy).decision is Decision.ALLOW_VIA_BRANCH


@pytest.mark.parametrize(
    "data",
    [
        {"class_decisions": {"unknown": "allow"}},
        {"class_decisions": {"out_of_scope": "ask"}},
        {"class_decisions": {"irreversible": "allow"}},
        {"class_decisions": {"irreversible": "allow_via_branch"}},
        {"class_decisions": {"read": "maybe"}},
        {"class_decisions": {"nonsense": "deny"}},
        {"class_decisions": ["read"]},
        {"mystery_key": 1},
        {"retention": {"default": {"min_snapshots": -1}}},
        {"retention": {"default": {"min_days": 3}}},
        {"retention": {"extra": {}}},
        {"retention": {"tables": {"orders": "7 days"}}},
        {"thresholds": {"max_affected_row_pct": "ten"}},
        {"thresholds": {"max_rows": 3}},
        {"protected_tables": "orders"},
        {"task_scope": {"tables": ["orders"]}},
    ],
)
def test_invalid_policies_are_rejected(data):
    with pytest.raises(PolicyError):
        Policy.from_dict(data)


def test_unreadable_or_malformed_files_raise_policy_error(tmp_path: Path):
    with pytest.raises(PolicyError):
        Policy.load(tmp_path / "missing.yaml")
    bad = tmp_path / "bad.yaml"
    bad.write_text("a: [unclosed")
    with pytest.raises(PolicyError):
        Policy.load(bad)
    listing = tmp_path / "list.yaml"
    listing.write_text(yaml.safe_dump(["not", "a", "mapping"]))
    with pytest.raises(PolicyError):
        Policy.load(listing)


def test_classification_with_no_statements_is_denied():
    empty = Classification(C.READ, (), (), (), None)
    assert Policy().decide(empty, SCOPE).decision is Decision.DENY
