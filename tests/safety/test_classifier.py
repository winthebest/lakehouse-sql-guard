from __future__ import annotations

import pytest

from sqlguard.core.catalog import LakehouseCatalog
from sqlguard.safety import (
    DropTable,
    ExpireSnapshots,
    FastForward,
    RemoveOrphanFiles,
    ReversibilityClass,
    RollbackToSnapshot,
    SqlAction,
    classify,
)
from sqlguard.safety.actions import MaintenanceAction
from sqlguard.safety.classifier import Impact

C = ReversibilityClass


def klass(sql: str, dialect: str = "duckdb") -> ReversibilityClass:
    return classify(SqlAction(sql, dialect)).klass


@pytest.mark.parametrize(
    "where",
    [
        "TRUE",
        "1 = 1",
        "1",
        "(1 = 1)",
        "NOT FALSE",
        "'a' = 'a'",
        "2 > 1",
        "id = id",
        "id = 3 OR 1 = 1",
        "(id = 3 OR TRUE) AND 1 = 1",
        "name LIKE '%'",
        "1 = 1 AND 2 = 2",
    ],
)
def test_tautological_where_is_treated_as_missing(where):
    result = classify(SqlAction(f"DELETE FROM orders WHERE {where}"))
    assert result.klass is C.RISKY_WRITE
    assert result.has_where is False


@pytest.mark.parametrize(
    "where",
    [
        "id = 3",
        "1 = 1 AND id = 3",
        "id > 0",
        "amount IS NOT NULL",
        "1 = 2 OR id = 3",
        "id IN (1, 2)",
    ],
)
def test_selective_where_stays_reversible(where):
    result = classify(SqlAction(f"UPDATE orders SET amount = 0 WHERE {where}"))
    assert result.klass is C.REVERSIBLE_WRITE
    assert result.has_where is True


def test_multi_statement_takes_the_most_severe_class():
    result = classify(SqlAction("SELECT 1; DELETE FROM orders WHERE id = 1; TRUNCATE TABLE orders"))
    assert [s.klass for s in result.statements] == [C.READ, C.REVERSIBLE_WRITE, C.RISKY_WRITE]
    assert result.klass is C.RISKY_WRITE
    assert result.write_tables == ("db.orders",)


def test_transaction_control_hides_nothing():
    result = classify(SqlAction("SELECT 1; COMMIT; DROP TABLE orders"))
    assert result.klass is C.UNKNOWN
    assert [s.klass for s in result.statements] == [C.READ, C.UNKNOWN, C.SCHEMA_BREAKING]


def test_cte_names_are_not_tables_but_real_references_are():
    result = classify(SqlAction("WITH t AS (SELECT * FROM orders) SELECT * FROM t"))
    assert result.tables == ("db.orders",)
    # A CTE that shadows a real table name still reads the real table inside its own body.
    shadow = classify(SqlAction("WITH orders AS (SELECT * FROM orders) SELECT * FROM orders"))
    assert "db.orders" in shadow.tables


def test_table_names_are_qualified_and_case_normalized():
    result = classify(SqlAction('SELECT * FROM Orders o JOIN "Items" i ON 1 = 1 JOIN x.Y ON 1 = 1'))
    assert result.tables == ("db.orders", "db.Items", "x.y")


def test_namespace_comes_from_catalog(catalog: LakehouseCatalog):
    other = LakehouseCatalog(catalog.catalog, namespace="analytics")
    result = classify(SqlAction("DELETE FROM orders WHERE id = 1"), other)
    assert result.write_tables == ("analytics.orders",)


def test_write_targets_are_separate_from_read_tables():
    result = classify(SqlAction("INSERT INTO orders SELECT * FROM order_items"))
    assert result.tables == ("db.orders", "db.order_items")
    assert result.write_tables == ("db.orders",)


def test_table_function_is_unknown_even_inside_a_write():
    assert klass("INSERT INTO orders SELECT * FROM read_csv('x.csv')") is C.UNKNOWN


def test_merge_branches_are_not_misread_as_unfiltered_dml():
    sql = (
        "MERGE INTO orders USING s ON orders.id = s.id "
        "WHEN MATCHED THEN UPDATE SET amount = s.amount "
        "WHEN NOT MATCHED THEN INSERT (id) VALUES (s.id)"
    )
    assert klass(sql) is C.REVERSIBLE_WRITE


def test_alter_takes_the_strictest_action():
    assert klass("ALTER TABLE orders ADD COLUMN c INT") is C.REVERSIBLE_WRITE
    assert klass("ALTER TABLE orders DROP COLUMN c") is C.SCHEMA_BREAKING


def test_unrecognised_dialect_or_garbage_fails_closed():
    assert klass("SELECT 1", dialect="no-such-dialect") is C.UNKNOWN
    assert klass("\x00\x01 not sql ((") is C.UNKNOWN
    assert klass("SELECT " + "(" * 5000 + "1" + ")" * 5000) in (C.READ, C.UNKNOWN)


def test_unsupported_action_object_is_unknown():
    result = classify("DELETE FROM orders")  # type: ignore[arg-type]  # deliberately wrong type
    assert result.klass is C.UNKNOWN


@pytest.mark.parametrize(
    ("action", "expected"),
    [
        (ExpireSnapshots("orders"), C.IRREVERSIBLE),
        (RemoveOrphanFiles("orders"), C.IRREVERSIBLE),
        (DropTable("orders", purge=True), C.IRREVERSIBLE),
        (DropTable("orders"), C.SCHEMA_BREAKING),
        (RollbackToSnapshot("orders", snapshot_id=1), C.RISKY_WRITE),
        (FastForward("orders", branch="main", to="audit"), C.RISKY_WRITE),
    ],
)
def test_structured_actions(action, expected):
    result = classify(action)
    assert result.klass is expected
    assert result.write_tables == ("db.orders",)
    assert result.statements[0].maintenance is action


def test_structured_action_without_table_is_unknown():
    assert classify(ExpireSnapshots("  ")).klass is C.UNKNOWN
    assert classify(MaintenanceAction("orders")).klass is C.UNKNOWN


def test_call_statement_yields_maintenance_action():
    result = classify(
        SqlAction("CALL system.expire_snapshots('Db.Orders', TIMESTAMP '2020-01-01')")
    )
    statement = result.statements[0]
    assert statement.klass is C.IRREVERSIBLE
    assert statement.tables == ("db.orders",)
    assert isinstance(statement.maintenance, ExpireSnapshots)


def test_with_impact_returns_a_copy():
    original = classify(SqlAction("DELETE FROM orders WHERE id = 1"))
    updated = original.with_impact(Impact(rows_affected=5, total_rows=10))
    assert original.est_impact is None
    assert updated.est_impact is not None and updated.est_impact.pct == 50.0
    assert Impact(rows_affected=1, total_rows=0).pct is None
