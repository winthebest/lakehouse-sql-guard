import pytest

from sqlguard.core.lineage import (
    LineageError,
    SourceColumn,
    column_lineage,
    full_lineage,
    output_columns,
    qualify_statement,
)


def test_join_alias_lineage(schema):
    q = qualify_statement(
        "SELECT l.user_id, s.total_spend AS spend FROM labels l JOIN user_stats s "
        "ON l.user_id = s.user_id",
        schema,
    )
    assert column_lineage(q, "spend", schema) == {SourceColumn("user_stats", "total_spend")}


def test_cte_subquery_and_star(schema):
    sql = """
        WITH s AS (SELECT user_id, total_spend * 2 AS doubled FROM user_stats)
        SELECT l.*, x.doubled AS spend,
               (SELECT max(total_spend) FROM user_stats u WHERE u.user_id = l.user_id) AS mx
        FROM labels l JOIN s x ON l.user_id = x.user_id
    """
    q = qualify_statement(sql, schema)
    assert output_columns(q) == ["user_id", "label_ts", "churned", "spend", "mx"]
    lineage = full_lineage(q, schema)
    assert lineage["spend"] == {SourceColumn("user_stats", "total_spend")}
    assert lineage["mx"] == {SourceColumn("user_stats", "total_spend")}
    assert lineage["churned"] == {SourceColumn("labels", "churned")}


def test_expression_with_multiple_sources(schema):
    q = qualify_statement(
        "SELECT s.total_spend * l.churned AS mixed FROM labels l JOIN user_stats s "
        "ON l.user_id = s.user_id",
        schema,
    )
    assert column_lineage(q, "mixed", schema) == {
        SourceColumn("user_stats", "total_spend"),
        SourceColumn("labels", "churned"),
    }


def test_unknown_column_raises(schema):
    with pytest.raises(LineageError):
        qualify_statement("SELECT nope FROM labels", schema)


def test_spark_dialect(schema):
    q = qualify_statement("SELECT user_id FROM labels", schema, dialect="spark")
    assert output_columns(q) == ["user_id"]
