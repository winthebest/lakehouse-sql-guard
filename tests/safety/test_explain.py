"""EXPLAIN shows a query plan: allowed for read-only queries, refused for everything else."""

from __future__ import annotations

import json

import pytest

from sqlguard.core.findings import Decision
from sqlguard.core.parsing import SqlParseError, explained, parse
from sqlguard.safety import ReversibilityClass, SqlAction, classify
from sqlguard.safety.guard import Outcome, redact_sql
from tests.safety.conftest import ROWS, main_ids

C = ReversibilityClass


@pytest.mark.parametrize(
    ("sql", "options", "analyze"),
    [
        ("EXPLAIN SELECT * FROM orders", "", False),
        ("explain analyze SELECT 1", "analyze", True),
        ("EXPLAIN (FORMAT json) SELECT 1", "(FORMAT json)", False),
        ("EXPLAIN (ANALYZE true, FORMAT json) SELECT 1", "(ANALYZE true, FORMAT json)", True),
        ("EXPLAIN EXTENDED SELECT 1", "EXTENDED", False),
    ],
)
def test_explain_is_split_into_options_and_statement(sql, options, analyze):
    explain = explained(parse(sql)[0].ast)
    assert explain is not None
    assert (explain.options, explain.analyze) == (options, analyze)
    assert explain.statement.kind == "Select"


def test_non_explain_and_bad_bodies():
    assert explained(parse("SELECT 1")[0].ast) is None
    with pytest.raises(SqlParseError):
        explained(parse("EXPLAIN")[0].ast)


@pytest.mark.parametrize(
    "sql",
    [
        "EXPLAIN SELECT * FROM orders",
        "EXPLAIN ANALYZE SELECT count(*) FROM orders",
        "EXPLAIN (FORMAT json) SELECT * FROM orders o JOIN customers c ON o.id = c.id",
        "EXPLAIN SELECT 1 UNION SELECT 2",
    ],
)
def test_explain_of_a_read_is_a_read_of_the_same_tables(sql):
    result = classify(SqlAction(sql))
    plain = classify(SqlAction(explained(parse(sql)[0].ast).statement.sql))
    assert result.klass is C.READ
    assert result.tables == plain.tables
    assert result.statements[0].verb == "EXPLAIN"


@pytest.mark.parametrize(
    "sql",
    [
        "EXPLAIN DELETE FROM orders",
        "EXPLAIN ANALYZE DELETE FROM orders",  # runs the delete
        "EXPLAIN ANALYZE UPDATE orders SET amount = 0",
        "EXPLAIN INSERT INTO orders SELECT * FROM orders",
        "EXPLAIN DROP TABLE orders",
        "EXPLAIN EXPLAIN SELECT 1",
        "EXPLAIN WITH d AS (DELETE FROM orders RETURNING *) SELECT * FROM d",
        "EXPLAIN SELECT * FROM read_csv('/etc/passwd')",
        "EXPLAIN (FORMAT json; DROP TABLE orders) SELECT 1",
        "EXPLAIN nonsense",
    ],
)
def test_explain_of_anything_else_is_not_a_read(sql):
    assert classify(SqlAction(sql)).klass is not C.READ


def test_each_statement_after_an_explain_is_judged_on_its_own():
    assert classify(SqlAction("EXPLAIN SELECT 1; SELECT 2")).klass is C.READ
    assert classify(SqlAction("EXPLAIN SELECT 1; DELETE FROM orders")).klass is not C.READ


def test_explain_runs_through_the_guard_without_writing(guard, orders, scope, audit_path):
    result = guard.submit(SqlAction("EXPLAIN SELECT count(*) FROM orders WHERE id > 3"), scope)
    assert result.outcome is Outcome.READ and result.decision is Decision.ALLOW
    assert result.data.num_rows >= 1
    assert len(main_ids(orders)) == 1
    record = [json.loads(line) for line in audit_path.read_text().splitlines()][-1]
    assert record["action"]["sql"].startswith("EXPLAIN SELECT")
    assert "3" not in record["action"]["sql"]  # literals stay redacted


def test_explain_analyze_of_a_delete_is_refused_and_deletes_nothing(guard, orders, scope):
    result = guard.submit(SqlAction("EXPLAIN ANALYZE DELETE FROM orders"), scope)
    assert result.decision is Decision.DENY and not result.executed
    assert orders.read("orders").num_rows == ROWS


def test_explain_of_an_out_of_scope_table_is_denied(guard, scope):
    result = guard.submit(SqlAction("EXPLAIN SELECT * FROM other.secret"), scope)
    assert result.decision is Decision.DENY and not result.executed


def test_redaction_keeps_the_explained_query():
    assert redact_sql("EXPLAIN ANALYZE SELECT * FROM t WHERE a = 'x'", "duckdb") == (
        "EXPLAIN ANALYZE SELECT * FROM t WHERE a = ?"
    )
