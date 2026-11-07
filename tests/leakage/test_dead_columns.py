"""A wall-clock call, label use or look-ahead whose value reaches no output column and decides no
row cannot reach a feature. Once a spec declares output roles it is shown as INFO, like one that
feeds only label or metadata columns; a spec without roles keeps the finding, and any path to a
feature column or to a clause that picks rows keeps it too."""

import pytest

from sqlguard.leakage.spec import LeakageSpec
from sqlguard.leakage.validator import static_findings, validate

from .conftest import SCHEMA, SPEC

ROLES = {"output_roles": {"label": ["churned"]}}


def report(sql: str, **overrides):
    spec = LeakageSpec.from_dict({**SPEC, **overrides})
    return validate(sql, spec, schema=SCHEMA)


def scored(sql: str, **overrides) -> set[str]:
    return {
        f.rule_id
        for f in report(sql, **overrides).findings
        if not f.evidence.get("non_feature_only")
    }


STAGE = """WITH staged AS (
  SELECT user_id, label_ts, churned, CURRENT_TIMESTAMP AS built_at, {extra} FROM labels
)
SELECT {select} FROM staged {tail}"""


def staged(select: str, extra: str = "1 AS one", tail: str = "") -> str:
    return STAGE.format(select=select, extra=extra, tail=tail)


DEAD = staged("user_id, label_ts, churned")


def test_a_clock_that_nothing_reads_is_info_once_roles_are_declared():
    got = report(DEAD, **ROLES)
    clock = [f for f in got.findings if f.rule_id == "L7"]
    assert clock and all(f.evidence.get("non_feature_only") for f in clock)
    assert got.verdict == "clean"


def test_the_same_query_keeps_the_finding_without_declared_roles():
    assert "L7" in scored(DEAD)


@pytest.mark.parametrize(
    "sql",
    [
        staged("user_id, label_ts, churned, built_at"),
        staged("user_id, label_ts, churned, EXTRACT(HOUR FROM built_at) AS hour"),
        staged("user_id, label_ts, churned", tail="WHERE built_at > TIMESTAMP '2020-01-01'"),
        staged("user_id, label_ts, churned", tail="QUALIFY built_at IS NOT NULL"),
        staged("user_id, label_ts, churned", tail="ORDER BY built_at LIMIT 10"),
        staged("user_id, label_ts, churned, COUNT(*) OVER (PARTITION BY built_at) AS n"),
        staged("user_id, label_ts, churned, ROW_NUMBER() OVER (ORDER BY built_at) AS n"),
        staged("user_id, label_ts, churned, COUNT(*) AS n", tail="GROUP BY ALL, built_at"),
        staged(
            "s.user_id, s.label_ts, s.churned", tail="s JOIN staged t ON t.built_at = s.built_at"
        ),
        staged("*"),
        "WITH staged AS (SELECT user_id, label_ts, churned, CURRENT_TIMESTAMP AS built_at "
        "FROM labels), more AS (SELECT * FROM staged) SELECT * FROM more",
        "WITH staged AS (SELECT user_id, label_ts, churned, CURRENT_TIMESTAMP AS built_at "
        "FROM labels) SELECT user_id, label_ts, churned FROM staged UNION ALL "
        "SELECT user_id, label_ts, churned FROM labels WHERE label_ts > (SELECT MAX(built_at) "
        "FROM staged)",
    ],
)
def test_a_clock_that_reaches_a_feature_or_a_row_clause_stays_flagged(sql):
    assert "L7" in scored(sql, **ROLES), sql


def test_a_look_ahead_that_nothing_reads_is_dead_too_and_one_that_is_read_is_not():
    window = "LEAD(churned) OVER (PARTITION BY user_id ORDER BY label_ts) AS next_one"
    dead = staged("user_id, label_ts, churned", extra=window)
    live = staged("user_id, label_ts, churned, next_one", extra=window)
    assert "L1" not in scored(dead, **ROLES)
    assert "L1" in scored(live, **ROLES)
    assert "L1" in scored(dead)


def test_an_unused_column_is_not_a_reason_to_trust_a_trace_that_could_not_finish():
    # a table function the trace does not follow
    sql = (
        "SELECT l.user_id, l.label_ts, l.churned, x.v FROM labels l, "
        "LATERAL (SELECT CURRENT_TIMESTAMP AS v) x"
    )
    assert "L7" in scored(sql, **ROLES)


def test_static_findings_expose_the_same_split():
    findings = static_findings(DEAD, LeakageSpec.from_dict({**SPEC, **ROLES}), SCHEMA)
    assert any(f.evidence.get("non_feature_only") and f.rule_id == "L7" for f in findings)


# --- ways a column is read that a trace by name does not see ------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        # the consumer renames the columns of the CTE it reads
        "WITH st AS (SELECT user_id, CURRENT_TIMESTAMP AS t FROM entities) "
        "SELECT l.user_id, l.label_ts, l.churned, date_diff('day', l.label_ts, a.tt) AS age "
        "FROM labels l JOIN st AS a(user_id, tt) ON a.user_id = l.user_id",
        "WITH st AS (SELECT user_id, CURRENT_TIMESTAMP AS t FROM entities) "
        "SELECT l.user_id, l.label_ts, l.churned FROM labels l "
        "JOIN st AS a(uid, tt) ON a.uid = l.user_id WHERE a.tt > l.label_ts",
        # a column read by its position (DuckDB)
        "WITH st AS (SELECT user_id, label_ts, churned, "
        "LEAD(churned) OVER (PARTITION BY user_id ORDER BY label_ts) AS nx FROM labels) "
        "SELECT st.user_id, st.label_ts, st.churned, #4 AS feat FROM st",
        "WITH st AS (SELECT user_id, label_ts, churned, CURRENT_TIMESTAMP AS t FROM labels) "
        "SELECT st.user_id, st.label_ts, st.churned FROM st WHERE #4 > st.label_ts",
    ],
)
def test_a_column_read_by_a_rename_or_a_position_is_not_dead(sql):
    got = scored(sql, **ROLES)
    assert got & {"L1", "L6", "L7", "PARSE"}, sql
