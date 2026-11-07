"""Two optional declarations: the column that closes a version, and keys that name one row.

`validity_end_columns` says a table's column holds the time the row's validity ended, in the same
clock as its time column (an SCD2 `valid_to` that is set when the successor is loaded, never a
scheduled future date). A bound on it then limits what was known the way a bound on the time
column does. `unique_keys` says which columns of a table never repeat, so two reads of the label
table joined on one are the same row.
"""

import pytest

from sqlguard.leakage.spec import LeakageSpec, SpecError
from sqlguard.leakage.validator import static_findings

from .conftest import SPEC

SCHEMA = {
    "labels": {
        "row_id": "BIGINT",
        "user_id": "BIGINT",
        "label_ts": "TIMESTAMP",
        "churned": "BIGINT",
        "x": "DOUBLE",
    },
    "plan_history": {
        "user_id": "BIGINT",
        "plan": "VARCHAR",
        "valid_from": "TIMESTAMP",
        "valid_to": "TIMESTAMP",
        "ingested_at": "TIMESTAMP",
    },
}
HISTORY = {
    **SPEC,
    "time_columns": {"plan_history": "valid_from"},
    "availability_columns": {"plan_history": "ingested_at"},
    "max_ingest_delay": {"plan_history": "8 days"},
    "static_tables": [],
    "outcome_columns": [],
}
VALIDITY = {"validity_end_columns": {"plan_history": "valid_to"}}


def findings(sql: str, **overrides) -> set[str]:
    spec = LeakageSpec.from_dict({**HISTORY, **overrides})
    return {f.rule_id for f in static_findings(sql, spec, SCHEMA)}


# --- validity_end_columns --------------------------------------------------------------------

CLOSED = """SELECT l.user_id, l.label_ts, l.churned,
       COUNT(p.user_id) AS n_completed,
       AVG(date_diff('day', p.valid_from, p.valid_to)) AS avg_days
FROM labels l
LEFT JOIN plan_history p ON p.user_id = l.user_id AND {cond}
GROUP BY ALL"""


def closed(cond: str) -> str:
    return CLOSED.format(cond=cond)


def test_a_version_closed_longer_ago_than_the_delay_was_already_known():
    sql = closed("p.valid_to < l.label_ts - INTERVAL 8 DAY")
    assert findings(sql, **VALIDITY) == set()
    assert findings(sql), "without the declaration the bound is not read"


@pytest.mark.parametrize(
    "cond",
    [
        "p.valid_to < l.label_ts - INTERVAL 2 DAY",
        "p.valid_to <= l.label_ts - INTERVAL 7 DAY",
        "p.valid_to < l.label_ts",
        "p.valid_to <= l.label_ts",
        "p.valid_to < l.label_ts + INTERVAL 1 DAY",
        "p.valid_to >= l.label_ts - INTERVAL 30 DAY",
        "p.valid_to > l.label_ts",
        "p.valid_to IS NULL OR p.valid_to >= l.label_ts",
    ],
)
def test_a_closing_bound_that_leaves_room_for_late_loads_is_still_flagged(cond):
    assert findings(closed(cond), **VALIDITY)


def test_a_lower_bound_on_the_closing_time_is_not_a_cap():
    assert findings(closed("p.valid_to >= l.label_ts - INTERVAL 30 DAY"), **VALIDITY)


def test_the_declaration_changes_nothing_for_queries_that_only_read_the_column():
    # static analysis does not follow the value of valid_to; replay decides these
    for sql in (
        """SELECT l.user_id, l.label_ts, l.churned,
       COALESCE(p.valid_to < l.label_ts, FALSE) AS ended
FROM labels l LEFT JOIN plan_history p
  ON p.user_id = l.user_id AND p.ingested_at < l.label_ts""",
        """SELECT l.user_id, l.label_ts, l.churned, p.plan
FROM labels l LEFT JOIN plan_history p
  ON p.user_id = l.user_id AND p.ingested_at < l.label_ts AND p.valid_from < l.label_ts
 AND (p.valid_to IS NULL OR p.valid_to >= l.label_ts)""",
        closed("p.valid_to >= l.label_ts - INTERVAL 30 DAY"),
    ):
        assert findings(sql, **VALIDITY) == findings(sql)


def test_a_declared_validity_column_needs_the_delay_like_the_time_column_does():
    sql = closed("p.valid_to < l.label_ts - INTERVAL 3 DAY")
    assert findings(sql, **VALIDITY) == {"L4"}
    assert findings(sql, **VALIDITY, max_ingest_delay={"plan_history": "3 days"}) == set()


def test_the_validity_column_must_belong_to_a_table_with_a_time_column():
    with pytest.raises(SpecError, match="validity_end_columns"):
        LeakageSpec.from_dict({**HISTORY, "validity_end_columns": {"events": "valid_to"}})
    with pytest.raises(SpecError, match="validity_end_columns"):
        LeakageSpec.from_dict({**HISTORY, "validity_end_columns": {"plan_history": "valid_from"}})
    with pytest.raises(SpecError, match="validity_end_columns"):
        LeakageSpec.from_dict({**HISTORY, "validity_end_columns": {"plan_history": "ingested_at"}})
    with pytest.raises(SpecError, match="validity_end_columns"):
        LeakageSpec.from_dict({**HISTORY, "validity_end_columns": ["valid_to"]})


def test_the_validity_column_is_checked_against_the_schema():
    spec = LeakageSpec.from_dict({**HISTORY, "validity_end_columns": {"plan_history": "ends"}})
    found = static_findings(closed("p.valid_from < l.label_ts - INTERVAL 8 DAY"), spec, SCHEMA)
    assert any(f.rule_id == "SPEC" for f in found)


def test_names_are_lowercased_and_the_field_is_optional():
    spec = LeakageSpec.from_dict({**HISTORY, "validity_end_columns": {"Plan_History": "Valid_To"}})
    assert spec.validity_end_columns == {"plan_history": "valid_to"}
    assert LeakageSpec.from_dict(HISTORY).validity_end_columns == {}


# --- unique_keys -----------------------------------------------------------------------------

ROW_SPEC = {
    "label_table": "labels",
    "entity_keys": ["user_id"],
    "prediction_ts": "label_ts",
    "label_columns": ["churned"],
    "time_columns": {"labels": "label_ts"},
    "label_known_after": "14 days",
}
SELF_JOIN = """WITH flags AS (SELECT row_id, CASE WHEN x > 1 THEN 1 ELSE 0 END AS big FROM labels)
SELECT l.user_id, l.label_ts, l.churned, f.big
FROM labels AS l JOIN flags AS f {join}"""


def row_findings(sql: str, **overrides) -> set[str]:
    spec = LeakageSpec.from_dict({**ROW_SPEC, **overrides})
    return {f.rule_id for f in static_findings(sql, spec, SCHEMA)}


@pytest.mark.parametrize(
    "join", ["USING (row_id)", "ON l.row_id = f.row_id", "ON f.row_id = l.row_id"]
)
def test_a_second_read_of_the_label_table_joined_on_a_unique_key_is_the_same_row(join):
    sql = SELF_JOIN.format(join=join)
    assert row_findings(sql), "without the declaration the second read is history"
    assert row_findings(sql, unique_keys={"labels": ["row_id"]}) == set()


@pytest.mark.parametrize(
    "join",
    [
        "ON l.user_id = f.row_id",
        "ON l.row_id = f.row_id + 1",
        "ON l.row_id <= f.row_id",
        "ON l.row_id = f.row_id OR l.user_id = 1",
        "ON TRUE",
    ],
)
def test_a_join_that_is_not_an_equality_on_the_key_stays_history(join):
    sql = SELF_JOIN.format(join=join)
    assert row_findings(sql, unique_keys={"labels": ["row_id"]})


def test_a_key_that_is_not_declared_unique_names_no_row():
    sql = SELF_JOIN.format(join="ON l.user_id = f.row_id").replace("f.row_id", "f.user_id")
    sql = sql.replace("SELECT row_id,", "SELECT user_id,")
    assert row_findings(sql, unique_keys={"labels": ["row_id"]})


def test_the_label_of_the_same_row_is_still_the_target():
    sql = """WITH flags AS (SELECT row_id, churned * 2 AS doubled FROM labels)
SELECT l.user_id, l.label_ts, l.churned, f.doubled
FROM labels AS l JOIN flags AS f USING (row_id)"""
    assert "L6" in row_findings(sql, unique_keys={"labels": ["row_id"]})


def test_unique_keys_accept_one_key_or_several_and_are_optional():
    one = LeakageSpec.from_dict({**ROW_SPEC, "unique_keys": {"Labels": ["Row_Id"]}})
    assert one.unique_keys == {"labels": (("row_id",),)}
    many = LeakageSpec.from_dict({**ROW_SPEC, "unique_keys": {"labels": [["row_id"], ["a", "b"]]}})
    assert many.unique_keys == {"labels": (("row_id",), ("a", "b"))}
    assert LeakageSpec.from_dict(ROW_SPEC).unique_keys == {}


@pytest.mark.parametrize(
    "value",
    [
        {"labels": []},
        {"labels": [[]]},
        {"labels": "row_id"},
        {"labels": [1]},
        {"labels": [["row_id"], "x"]},
        {"unknown_table": ["id"]},
        ["row_id"],
    ],
)
def test_malformed_unique_keys_are_rejected(value):
    with pytest.raises(SpecError, match="unique_keys"):
        LeakageSpec.from_dict({**ROW_SPEC, "unique_keys": value})


# --- the key may only stand for the row when both sides are the same key -----------------------

KEYED = {
    **ROW_SPEC,
    "unique_keys": {"labels": [["row_id"], ["other_id"]]},
}
KEYED_SCHEMA = {"labels": {**SCHEMA["labels"], "other_id": "BIGINT"}}


def keyed_findings(sql: str, schema=KEYED_SCHEMA, **overrides) -> set[str]:
    spec = LeakageSpec.from_dict({**KEYED, **overrides})
    return {f.rule_id for f in static_findings(sql, spec, schema)}


@pytest.mark.parametrize(
    "join",
    [
        "ON f.other_id = l.row_id",
        "ON f.row_id = l.other_id",
        "ON f.row_id = l.label_ts",
        "ON f.label_ts = l.row_id",
        "ON f.row_id = l.user_id",
    ],
)
def test_two_different_keys_or_a_key_and_a_time_are_not_the_same_row(join):
    sql = f"SELECT l.user_id, l.label_ts, l.churned, f.x AS v FROM labels l JOIN labels f {join}"
    assert keyed_findings(sql), join
    assert keyed_findings(sql.replace(" JOIN labels f", " LEFT JOIN labels f"))


def test_a_subquery_on_the_key_is_not_the_spine_row_either():
    sql = (
        "SELECT l.user_id, l.label_ts, l.churned, "
        "EXISTS (SELECT 1 FROM labels f WHERE f.row_id = l.row_id AND f.churned = 1) AS v "
        "FROM labels l"
    )
    assert keyed_findings(sql)
    scalar = sql.replace("EXISTS (SELECT 1 FROM", "(SELECT MAX(f.churned) FROM").replace(
        " AND f.churned = 1", ""
    )
    assert keyed_findings(scalar)


def test_the_same_key_on_both_sides_still_makes_the_second_read_the_same_row():
    sql = SELF_JOIN.format(join="ON l.row_id = f.row_id")
    assert row_findings(sql, unique_keys={"labels": [["row_id"], ["other_id"]]}) == set()


def test_a_label_filter_on_the_nullable_side_of_a_left_join_is_a_feature_not_a_row_choice():
    sql = (
        "SELECT l.user_id, l.label_ts, l.churned, f.row_id IS NOT NULL AS v FROM labels l "
        "LEFT JOIN (SELECT row_id FROM labels WHERE churned = 1) f ON f.row_id = l.row_id"
    )
    assert row_findings(sql, unique_keys={"labels": ["row_id"]})
    cte = (
        "WITH f AS (SELECT row_id FROM labels WHERE churned = 1) "
        "SELECT l.user_id, l.label_ts, l.churned, f.row_id IS NOT NULL AS v FROM labels l "
        "LEFT JOIN f USING (row_id)"
    )
    assert row_findings(cte, unique_keys={"labels": ["row_id"]})


def test_an_inner_join_on_the_key_still_makes_the_second_read_the_same_row():
    sql = SELF_JOIN.format(join="ON l.row_id = f.row_id")
    assert row_findings(sql, unique_keys={"labels": ["row_id"]}) == set()
    assert row_findings(
        sql.replace(" JOIN flags", " LEFT JOIN flags"), unique_keys={"labels": ["row_id"]}
    )
