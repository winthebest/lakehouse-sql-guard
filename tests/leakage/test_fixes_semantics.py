"""What a rewrite does to the values and rows of the query, judged by running both versions.

Replay and the static check say the repaired query reads nothing from the future. They cannot say
it still computes the same thing: both runs would compute the same wrong value. So each Fix also
carries `semantics`, and these tests execute the original and the repaired SQL on a fixture."""

from datetime import datetime

import duckdb
import pytest

from sqlguard.leakage.fixes import Fix, Semantics, Verification, suggest_fix
from sqlguard.leakage.rewrite import ROWS_KEPT, ROWS_MAY_DROP
from sqlguard.leakage.spec import LeakageSpec
from sqlguard.leakage.validator import LeakageReport, validate

from .conftest import SCHEMA, SPEC

SEL = "SELECT l.user_id, l.label_ts, l.churned"
G = "GROUP BY 1, 2, 3"
T = datetime(2026, 2, 1)


@pytest.fixture(scope="module")
def db():
    """User 1: two events before the prediction time and one after. User 2: only an event after
    it. User 3: no events at all. User 4 has events and no `entities` row."""
    conn = duckdb.connect()
    conn.execute(
        "CREATE TABLE labels AS SELECT * FROM (VALUES (1, TIMESTAMP '2026-02-01', 0), "
        "(2, TIMESTAMP '2026-02-01', 1), (3, TIMESTAMP '2026-02-01', 0), "
        "(4, TIMESTAMP '2026-02-01', 1)) t(user_id, label_ts, churned)"
    )
    conn.execute(
        "CREATE TABLE events AS SELECT * FROM (VALUES "
        "(1, 1, TIMESTAMP '2026-01-10', TIMESTAMP '2026-01-11', 5.0), "
        "(2, 1, TIMESTAMP '2026-01-20', TIMESTAMP '2026-01-21', 7.0), "
        "(3, 1, TIMESTAMP '2026-02-10', TIMESTAMP '2026-02-11', 9.0), "
        "(4, 2, TIMESTAMP '2026-02-15', TIMESTAMP '2026-02-16', 3.0), "
        "(5, 4, TIMESTAMP '2026-01-05', TIMESTAMP '2026-01-06', 2.0)) "
        "t(event_id, user_id, event_ts, ingested_at, amount)"
    )
    conn.execute(
        "CREATE TABLE entities AS SELECT * FROM (VALUES (1, 'a', TIMESTAMP '2025-01-01'), "
        "(2, 'b', TIMESTAMP '2025-01-01'), (3, 'c', TIMESTAMP '2025-01-01')) "
        "t(user_id, country, signup_ts)"
    )
    conn.execute(
        "CREATE TABLE user_stats AS SELECT * FROM (VALUES "
        "(1, TIMESTAMP '2026-01-01', 1.0, 1), (1, TIMESTAMP '2026-01-15', 2.0, 2), "
        "(1, TIMESTAMP '2026-01-20', 4.0, 3), (1, TIMESTAMP '2026-01-25', 8.0, 4), "
        "(1, TIMESTAMP '2026-02-05', 16.0, 5)) t(user_id, updated_at, total_spend, n_orders)"
    )
    return conn


def spec_with(**overrides) -> LeakageSpec:
    return LeakageSpec.from_dict({**SPEC, **overrides})


def fixes_of(sql: str, **overrides) -> list[Fix]:
    return suggest_fix(sql, spec_with(**overrides), schema=SCHEMA)


def last(sql: str, **overrides) -> Fix:
    rewrites = [f for f in fixes_of(sql, **overrides) if f.rewrites]
    assert rewrites
    return rewrites[-1]


def rows(db, sql: str) -> list[tuple]:
    return sorted(db.sql(sql).fetchall())


# --- a count over a LEFT JOIN counts the joined side, whatever the JOIN was before -------------


@pytest.mark.parametrize("join", ["LEFT JOIN", "JOIN"])
@pytest.mark.parametrize("count", ["COUNT(*)", "COUNT(1)", "COUNT(l.user_id)"])
def test_a_spine_row_without_past_events_counts_zero(db, join, count):
    sql = f"{SEL}, {count} n FROM labels l {join} events e ON e.user_id = l.user_id {G}"
    fix = last(sql)
    assert "COUNT(e.user_id)" in fix.after_sql
    assert dict((r[0], r[3]) for r in rows(db, fix.after_sql)) == {1: 2, 2: 0, 3: 0, 4: 1}
    assert any("counts 0, not 1" in c for c in fix.caveats)
    assert fix.semantics is Semantics.CHANGED


def test_an_aggregate_that_counts_the_unmatched_row_is_named_not_guessed():
    fix = last(f"{SEL}, SUM(1) n FROM labels l LEFT JOIN events e ON e.user_id = l.user_id {G}")
    assert any("SUM(1) still counts the spine row" in c for c in fix.caveats)


def test_counts_that_already_read_the_joined_table_are_left_alone(db):
    sql = (
        f"{SEL}, COUNT(e.event_id) n FROM labels l LEFT JOIN events e ON e.user_id = l.user_id {G}"
    )
    fix = last(sql)
    assert "COUNT(e.event_id)" in fix.after_sql
    assert fix.semantics is Semantics.PRESERVED and fix.caveats == []
    assert dict((r[0], r[3]) for r in rows(db, fix.after_sql)) == {1: 2, 2: 0, 3: 0, 4: 1}


# --- a window frame keeps its width, behind the current row ------------------------------------


def frame_sql(frame: str, kind: str = "ROWS") -> str:
    return (
        "WITH f AS (SELECT user_id, updated_at, SUM(total_spend) OVER (PARTITION BY user_id "
        f"ORDER BY updated_at {kind} BETWEEN {frame}) AS v FROM user_stats) "
        f"{SEL}, f.v FROM labels l ASOF JOIN f ON f.user_id = l.user_id "
        "AND l.label_ts > f.updated_at"
    )


@pytest.mark.parametrize(
    "before, after",
    [
        ("1 FOLLOWING AND 3 FOLLOWING", "3 PRECEDING AND 1 PRECEDING"),
        ("2 FOLLOWING AND 2 FOLLOWING", "2 PRECEDING AND 2 PRECEDING"),
        ("CURRENT ROW AND UNBOUNDED FOLLOWING", "UNBOUNDED PRECEDING AND CURRENT ROW"),
        ("CURRENT ROW AND 2 FOLLOWING", "2 PRECEDING AND CURRENT ROW"),
        ("1 FOLLOWING AND UNBOUNDED FOLLOWING", "UNBOUNDED PRECEDING AND 1 PRECEDING"),
        ("3 PRECEDING AND 2 FOLLOWING", "3 PRECEDING AND CURRENT ROW"),
        ("UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING", "UNBOUNDED PRECEDING AND CURRENT ROW"),
    ],
)
def test_a_following_frame_becomes_the_same_width_behind_and_says_so(before, after):
    fix = last(frame_sql(before))
    assert f"ROWS BETWEEN {after}" in " ".join(fix.after_sql.split())
    assert fix.semantics is Semantics.CHANGED  # never `preserved`: other rows are summed
    text = " ".join(fix.caveats)
    assert f"ROWS BETWEEN {before}" in text and f"ROWS BETWEEN {after}" in text
    assert fix.verification is Verification.STATIC_ONLY


def test_the_mirrored_frame_sums_the_previous_rows_not_all_earlier_ones(db):
    fix = last(frame_sql("1 FOLLOWING AND 3 FOLLOWING"))
    # at 2026-02-01 the versions known are 1, 2, 4, 8 (the ASOF row is the 8): the three before it
    # are 1.0 + 2.0 + 4.0
    assert rows(db, fix.after_sql)[0][3] == 7.0


@pytest.mark.parametrize("kind", ["RANGE", "GROUPS"])
def test_other_frame_kinds_are_explained_not_rewritten(kind):
    fixes = fixes_of(frame_sql("1 FOLLOWING AND 3 FOLLOWING", kind))
    assert not any(f.rewrites for f in fixes)


# --- a row-loss caveat is true without a catalog ----------------------------------------------


def test_inner_join_read_by_another_inner_join_does_not_claim_to_keep_every_row(db):
    sql = (
        f"{SEL}, COUNT(*) n FROM labels l JOIN events e ON e.user_id = l.user_id "
        f"JOIN entities x ON x.user_id = e.user_id {G}"
    )
    fix = last(sql)
    assert len(rows(db, fix.after_sql)) < len(rows(db, sql)) + 4  # rows do not all survive
    assert any(c.startswith(ROWS_MAY_DROP) and "INNER JOIN to x" in c for c in fix.caveats)
    assert not any(c.startswith(ROWS_KEPT) for c in fix.caveats)


def test_inner_join_with_a_where_on_the_joined_table_does_not_claim_to_keep_every_row(db):
    sql = (
        f"{SEL}, SUM(e.amount) a FROM labels l JOIN events e ON e.user_id = l.user_id "
        f"WHERE e.amount > 2 {G}"
    )
    fix = last(sql)
    # the WHERE keeps only users with a past event over 2: users 2, 3 and 4 still drop
    assert {r[0] for r in rows(db, fix.after_sql)} == {1}
    assert any(c.startswith(ROWS_MAY_DROP) and "WHERE reads e" in c for c in fix.caveats)
    assert not any(c.startswith(ROWS_KEPT) for c in fix.caveats)


def test_inner_join_nothing_else_reads_keeps_every_row_and_says_so(db):
    sql = f"{SEL}, SUM(e.amount) a FROM labels l JOIN events e ON e.user_id = l.user_id {G}"
    fix = last(sql)
    assert len(rows(db, fix.after_sql)) == 4
    assert any(c.startswith(ROWS_KEPT) for c in fix.caveats)
    assert not any(c.startswith(ROWS_MAY_DROP) for c in fix.caveats)


LATEST_STATS = (
    "(SELECT * FROM user_stats QUALIFY ROW_NUMBER() OVER "
    "(PARTITION BY user_id ORDER BY updated_at DESC) = 1)"
)
LATEST_EVENT = (
    "(SELECT * FROM events QUALIFY ROW_NUMBER() OVER "
    "(PARTITION BY user_id ORDER BY event_ts DESC) = 1)"
)


def test_a_ranking_is_not_put_in_a_query_that_joins_something_else(db):
    """A ROW_NUMBER over (entity, prediction time) in the outer SELECT would cut the rows of a
    second 1:N join down to one per spine row."""
    sql = (
        f"{SEL}, ev.amount, e2.event_id FROM labels l JOIN {LATEST_EVENT} ev "
        "ON ev.user_id = l.user_id LEFT JOIN events e2 ON e2.user_id = l.user_id "
        "AND e2.ingested_at < l.label_ts"
    )
    fixes = fixes_of(sql)
    assert not any(f.rewrites for f in fixes)
    assert fixes and all(f.semantics is Semantics.UNKNOWN for f in fixes)


def test_the_ranking_ends_in_the_tables_columns_so_ties_pick_the_same_row():
    fix = last(
        f"{SEL}, ev.amount FROM labels l LEFT JOIN {LATEST_EVENT} ev ON ev.user_id = l.user_id"
    )
    order = " ".join(fix.after_sql.split())
    assert "ORDER BY ev.event_ts DESC, ev.ingested_at DESC, ev.event_id" in order


def test_the_wording_for_snowflake_names_the_asof_join_not_a_left_join():
    sql = (
        f"{SEL}, s.total_spend FROM labels l JOIN "
        "(SELECT * FROM user_stats QUALIFY ROW_NUMBER() OVER "
        "(PARTITION BY user_id ORDER BY updated_at DESC) = 1) s ON s.user_id = l.user_id"
    )
    fix = last(sql, dialect="snowflake")
    text = " ".join(fix.caveats)
    assert "INNER JOIN became ASOF JOIN" in text and "no LEFT form" in text
    assert "became LEFT JOIN" not in text


# --- the meaning of a rewrite is its own field --------------------------------------------------


@pytest.mark.parametrize(
    "sql, semantics",
    [
        (
            f"{SEL}, SUM(e.amount) a FROM labels l LEFT JOIN events e ON e.user_id = l.user_id {G}",
            "preserved",
        ),
        (
            f"{SEL}, SUM(e.amount) a FROM labels l LEFT JOIN events e ON e.user_id = l.user_id "
            f"AND e.ingested_at <= l.label_ts {G}",
            "preserved",
        ),
        (
            f"{SEL}, SUM(e.amount) a FROM labels l LEFT JOIN events e ON e.user_id = l.user_id "
            f"AND e.event_ts < l.label_ts {G}",
            "preserved",
        ),
        (
            f"{SEL}, SUM(e.amount) a FROM labels l JOIN events e ON e.user_id = l.user_id {G}",
            "changed",
        ),
        (
            f"{SEL}, SUM(e.amount) a FROM labels l LEFT JOIN events e ON e.user_id = l.user_id "
            f"AND e.event_ts >= l.label_ts {G}",
            "changed",
        ),
        (
            f"{SEL}, s.total_spend FROM labels l JOIN {LATEST_STATS} s ON s.user_id = l.user_id",
            "changed",
        ),
    ],
)
def test_semantics_is_preserved_only_when_a_bound_is_all_that_was_added(sql, semantics):
    meanings = {f.semantics.value for f in fixes_of(sql) if f.rewrites}
    assert meanings == {semantics} or (semantics == "changed" and "changed" in meanings)


def test_a_fix_that_is_only_explained_has_unknown_semantics():
    fixes = fixes_of(
        f"{SEL}, SUM(r.refund_after_churn) x FROM labels l LEFT JOIN user_outcomes r "
        f"ON r.user_id = l.user_id {G}"
    )
    assert fixes and all(f.semantics is Semantics.UNKNOWN for f in fixes)


def test_fixes_carry_both_fields_in_json():
    data = last(
        f"{SEL}, SUM(e.amount) a FROM labels l LEFT JOIN events e ON e.user_id = l.user_id {G}"
    ).to_dict()
    assert data["verification"] == "static_only" and data["semantics"] == "preserved"


# --- Snowflake MATCH_CONDITION written by the user ---------------------------------------------

MATCH = (
    f"{SEL}, s.total_spend FROM labels l ASOF JOIN user_stats s "
    "MATCH_CONDITION ({cond}) ON s.user_id = l.user_id"
)


@pytest.mark.parametrize(
    "cond, verdict, rule",
    [
        ("l.label_ts > s.updated_at", "clean", None),
        ("l.label_ts > s.updated_at + INTERVAL '3 days'", "clean", None),
        ("l.label_ts >= s.updated_at", "leaky", "L5"),
        ("l.label_ts < s.updated_at", "leaky", "L1"),
    ],
)
def test_the_checker_reads_the_inequality_of_a_snowflake_asof_join(cond, verdict, rule):
    report = validate(MATCH.format(cond=cond), spec_with(dialect="snowflake"), schema=SCHEMA)
    assert report.verdict == verdict
    assert ({f.rule_id for f in report.findings} == {rule}) if rule else not report.findings


def test_a_snowflake_asof_with_a_forward_condition_is_repaired_in_snowflake_syntax():
    fix = last(MATCH.format(cond="l.label_ts < s.updated_at"), dialect="snowflake")
    assert "MATCH_CONDITION (l.label_ts > s.updated_at)" in " ".join(fix.after_sql.split())


def test_a_finding_the_rewriter_no_longer_sees_is_still_explained():
    """If the report flags a leak and the rewrite-time reading finds nothing, say so."""
    sql = (
        f"{SEL}, SUM(e.amount) a FROM labels l LEFT JOIN events e ON e.user_id = l.user_id "
        f"AND e.event_ts < l.label_ts AND e.ingested_at < l.label_ts {G}"
    )
    spec = spec_with()
    clean = validate(sql, spec, schema=SCHEMA)
    flagged = validate(sql.replace(" AND e.ingested_at < l.label_ts", ""), spec, schema=SCHEMA)
    report = LeakageReport("leaky", flagged.score, flagged.findings)
    assert clean.verdict == "clean"
    fixes = suggest_fix(sql, spec, report, schema=SCHEMA)
    assert fixes and not any(f.rewrites for f in fixes)
    assert "no longer sees" in fixes[0].explanation
