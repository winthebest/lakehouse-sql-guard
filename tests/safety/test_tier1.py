"""Tier-1 labeled cases double as the regression suite for the static guard."""

from __future__ import annotations

import random

import pytest

from bench.safety.tier1 import (
    DEFAULT_POLICY,
    NOW,
    Case,
    evaluate,
    load_cases,
    summarize,
)
from sqlguard.core.findings import Decision
from sqlguard.safety import Policy, ReversibilityClass, SqlAction, TaskScope, classify

CASES = load_cases()
POLICY = Policy.load(DEFAULT_POLICY)


def test_suite_is_large_and_covers_every_class_and_the_evasion_set():
    assert len(CASES) >= 60
    assert {c.klass for c in CASES} == set(ReversibilityClass)
    tags = {t for c in CASES for t in c.tags}
    assert {"evasion", "multi-statement", "cte", "comment", "casing", "maintenance"} <= tags


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.id)
def test_case_matches_label(case: Case):
    decision = POLICY.decide(classify(case.action), case.scope, now=NOW)
    assert (decision.klass, decision.decision) == (case.klass, case.decision), decision.reasons


def test_no_dangerous_case_is_allowed():
    results = evaluate(CASES, POLICY)
    dangerous = [r for r in results if r.case.dangerous]
    assert dangerous
    assert [r.case.id for r in dangerous if r.decision is Decision.ALLOW] == []


def test_writes_are_never_plain_allow():
    results = evaluate(CASES, POLICY)
    writes = [r for r in results if r.case.klass is not ReversibilityClass.READ]
    assert all(r.decision is not Decision.ALLOW for r in writes)


def test_every_decision_carries_a_reason():
    assert all(r.reasons for r in evaluate(CASES, POLICY))


def test_few_safe_cases_are_blocked():
    summary = summarize(evaluate(CASES, POLICY))
    assert summary["safe_blocked"] / summary["safe_total"] <= 0.10
    assert summary["failures"] == []


def test_classification_is_fast():
    assert summarize(evaluate(CASES, POLICY))["latency_ms"]["median"] < 50


DANGEROUS_SQL = [
    "DELETE FROM orders",
    "DELETE FROM orders WHERE 1 = 1",
    "UPDATE orders SET amount = 0",
    "TRUNCATE TABLE orders",
    "DROP TABLE orders",
    "DROP TABLE orders PURGE",
    "ALTER TABLE orders DROP COLUMN amount",
    "CREATE OR REPLACE TABLE orders AS SELECT 1 AS id",
    "INSERT OVERWRITE orders SELECT * FROM order_items",
    "CALL system.expire_snapshots('db.orders', TIMESTAMP '2020-01-01 00:00:00', 1)",
    "CALL system.remove_orphan_files('db.orders')",
]


def _mutate(sql: str, rng: random.Random) -> str:
    """Randomize case and whitespace and wrap with comments and a leading harmless statement."""
    cased = "".join(c.upper() if rng.random() < 0.5 else c.lower() for c in sql)
    spaced = cased.replace(" ", rng.choice([" ", "  ", "\n", "\t", " /* x */ "]))
    prefix = rng.choice(["", "SELECT 1; ", "/* hi */ ", "-- note\n", "SELECT 1;\n-- c\n"])
    suffix = rng.choice(["", ";", " -- done", " /* end */", "; SELECT 2"])
    return prefix + spaced + suffix


def test_obfuscated_dangerous_sql_is_never_allowed():
    rng = random.Random(20261104)
    scope = TaskScope.of(namespaces=["db"])
    for sql in DANGEROUS_SQL:
        for _ in range(40):
            mutated = _mutate(sql, rng)
            decision = POLICY.decide(classify(SqlAction(mutated)), scope, now=NOW)
            assert decision.decision in (Decision.ASK, Decision.DENY), mutated
            assert decision.reasons
