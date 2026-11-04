"""A CTE name means something only where the CTE is visible.

The classifier decides which tables a statement reads, and the Trino rewriter decides which names it
may leave unqualified. Both used to trust a CTE name found anywhere in the statement, so a CTE
defined in a subquery hid the real table of the same name used outside it.
"""

from __future__ import annotations

import pytest
from sqlglot import exp, parse_one

from sqlguard.core.findings import Decision
from sqlguard.safety import Policy, ReversibilityClass, SqlAction, TaskScope, classify
from sqlguard.safety.classifier import is_cte_reference
from sqlguard.safety.executor.base import ExecutionError
from sqlguard.safety.executor.trino_iceberg import trino_statement

C = ReversibilityClass


def tables(sql: str) -> tuple[str, ...]:
    return classify(SqlAction(sql)).tables


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        # visible in the body of the query that owns the WITH
        ("WITH t AS (SELECT 1 AS id) SELECT * FROM t", ()),
        # a name defined inside a subquery is not visible outside it
        (
            "SELECT * FROM secrets WHERE id IN (WITH secrets AS (SELECT 1 AS id) "
            "SELECT id FROM secrets)",
            ("db.secrets",),
        ),
        (
            "SELECT * FROM (WITH t AS (SELECT 1 AS id) SELECT * FROM t) a JOIN t ON t.id = a.id",
            ("db.t",),
        ),
        (
            "SELECT * FROM (WITH t AS (SELECT 1 AS id) SELECT * FROM t) a "
            "UNION ALL SELECT * FROM t",
            ("db.t",),
        ),
        # a CTE written inside another CTE is visible only there
        (
            "WITH a AS (WITH inner_t AS (SELECT 1 AS id) SELECT * FROM inner_t) "
            "SELECT * FROM a JOIN inner_t ON TRUE",
            ("db.inner_t",),
        ),
        # visible in CTEs written after it, not in the ones before it
        ("WITH a AS (SELECT 1 AS id), b AS (SELECT * FROM a) SELECT * FROM b", ()),
        ("WITH b AS (SELECT * FROM a), a AS (SELECT 1 AS id) SELECT * FROM b", ("db.a",)),
        # its own name inside its own body is a real table, as in the engines
        ("WITH orders AS (SELECT * FROM orders) SELECT * FROM orders", ("db.orders",)),
        # ... except under WITH RECURSIVE
        (
            "WITH RECURSIVE n AS (SELECT 1 AS i UNION ALL SELECT i + 1 FROM n WHERE i < 3) "
            "SELECT * FROM n",
            (),
        ),
        # the same name defined in two places stays visible where each one is
        (
            "WITH t AS (SELECT 1 AS id) SELECT * FROM t WHERE id IN "
            "(WITH t AS (SELECT 2 AS id) SELECT id FROM t)",
            (),
        ),
        # a qualified name is never a CTE
        ("WITH orders AS (SELECT 1 AS id) SELECT * FROM db.orders", ("db.orders",)),
        # sibling subqueries do not see each other's CTEs
        (
            "SELECT * FROM (WITH t AS (SELECT 1 AS id) SELECT * FROM t) a, (SELECT * FROM t) b",
            ("db.t",),
        ),
    ],
)
def test_a_cte_is_trusted_only_where_it_is_visible(sql: str, expected: tuple[str, ...]) -> None:
    assert tables(sql) == expected


def test_writes_see_the_ctes_of_their_own_statement_only() -> None:
    own = classify(
        SqlAction(
            "WITH keep AS (SELECT id FROM audit_log) DELETE FROM orders "
            "WHERE id IN (SELECT id FROM keep)"
        )
    )
    assert sorted(own.tables) == ["db.audit_log", "db.orders"]
    hidden = classify(
        SqlAction(
            "DELETE FROM orders WHERE id IN (WITH secrets AS (SELECT 1 AS id) "
            "SELECT id FROM secrets) AND id IN (SELECT id FROM secrets)"
        )
    )
    assert sorted(hidden.tables) == ["db.orders", "db.secrets"]
    assert hidden.write_tables == ("db.orders",)


def test_an_inner_cte_name_cannot_launder_a_table_outside_the_task_scope() -> None:
    scope = TaskScope.of(tables=["orders"])
    policy = Policy()
    plain = policy.decide(classify(SqlAction("SELECT * FROM secrets")), scope)
    assert plain.decision is Decision.DENY
    laundered = classify(
        SqlAction(
            "SELECT * FROM secrets WHERE id IN (WITH secrets AS (SELECT 1 AS id) "
            "SELECT id FROM secrets)"
        )
    )
    decided = policy.decide(laundered, scope)
    assert decided.decision is Decision.DENY and decided.klass is C.OUT_OF_SCOPE
    assert any("db.secrets" in reason for reason in decided.reasons)


def test_is_cte_reference_looks_at_the_given_node_only() -> None:
    tree = parse_one(
        "WITH t AS (SELECT 1 AS id) SELECT * FROM t JOIN db.t AS q ON TRUE", dialect="duckdb"
    )
    by_text = {node.sql(): is_cte_reference(node) for node in tree.find_all(exp.Table)}
    assert by_text == {"t": True, "db.t AS q": False}


# -- the Trino rewriter shares the rule -----------------------------------------------------------


def render(sql: str, readable: tuple[str, ...] = ("db.orders",)) -> str:
    return trino_statement(
        parse_one(sql, dialect="duckdb"),
        trino_catalog="lk",
        default_namespace="db",
        readable=readable,
    )


def test_trino_leaves_a_visible_cte_unqualified_and_qualifies_the_rest() -> None:
    sql = render("WITH o AS (SELECT * FROM orders) SELECT * FROM o")
    assert 'FROM "lk"."db"."orders"' in sql and "FROM o" in sql


def test_trino_does_not_leave_a_name_unqualified_because_a_subquery_defines_it() -> None:
    with pytest.raises(ExecutionError, match="not among the tables cleared"):
        render(
            "SELECT * FROM secrets WHERE id IN (WITH secrets AS (SELECT 1 AS id) "
            "SELECT id FROM secrets)"
        )
    with pytest.raises(ExecutionError, match="not among the tables cleared"):
        render("SELECT * FROM (WITH t AS (SELECT 1 AS id) SELECT * FROM t) a JOIN t ON t.id = a.id")


def test_trino_qualifies_a_cleared_table_that_a_subquery_cte_shadows() -> None:
    sql = render(
        "SELECT * FROM orders o WHERE o.id IN (WITH orders AS (SELECT 1 AS id) "
        "SELECT id FROM orders)"
    )
    assert sql.count('"lk"."db"."orders"') == 1  # the outer one only
    assert "FROM orders)" in sql  # the inner reference is the CTE
