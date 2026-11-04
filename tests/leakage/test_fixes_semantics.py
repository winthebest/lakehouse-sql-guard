"""What a rewrite does to the values and rows of the query, judged by running both versions.

Replay and the static check say the repaired query reads nothing from the future. They cannot say
it still computes the same thing: both runs would compute the same wrong value. So each Fix also
carries `semantics`, and these tests execute the original and the repaired SQL on a fixture."""

from collections import Counter
from datetime import datetime

import duckdb
import pytest

from sqlguard.leakage.fixes import Fix, Semantics, Verification, suggest_fix
from sqlguard.leakage.rewrite import ROWS_CHANGE, ROWS_KEPT, ROWS_MAY_DROP
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
    assert any("SUM(1) gives a spine row" in c for c in fix.caveats)
    assert fix.semantics is Semantics.CHANGED


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


# --- the label and the caveats are decided from the text, with or without a catalog -------------

LJ = "FROM labels l LEFT JOIN events e ON e.user_id = l.user_id"

DROPPING = {
    "where on the joined table": (
        f"{SEL}, SUM(e.amount) a {LJ} WHERE e.amount > 0 {G}",
        {1, 4},
    ),
    "having on a count of the joined table": (
        f"{SEL}, SUM(e.amount) a {LJ} {G} HAVING COUNT(e.event_id) > 2",
        set(),
    ),
    "bound inside an IN subquery": (
        f"{SEL} FROM labels l WHERE l.user_id IN "
        "(SELECT e.user_id FROM events e WHERE e.amount > 2)",
        {1},
    ),
    "bound inside an EXISTS subquery": (
        f"{SEL} FROM labels l WHERE EXISTS (SELECT 1 FROM events e WHERE e.user_id = l.user_id)",
        {1, 4},
    ),
    "bound inside a comparison with a scalar subquery": (
        f"{SEL} FROM labels l "
        "WHERE (SELECT COUNT(*) FROM events e WHERE e.user_id = l.user_id) > 2",
        set(),
    ),
    "having on the number of joined rows": (
        f"{SEL}, SUM(e.amount) a {LJ} {G} HAVING COUNT(*) > 2",
        set(),
    ),
}


@pytest.mark.parametrize("sql, kept", DROPPING.values(), ids=DROPPING.keys())
def test_a_bound_that_drops_spine_rows_says_so_without_replay(db, sql, kept):
    fixes = fixes_of(sql)  # schema only: no catalog, no replay
    fix = [f for f in fixes if f.rewrites][-1]
    assert fix.verification is Verification.STATIC_ONLY
    assert fix.semantics is Semantics.CHANGED  # never `preserved`
    assert any(c.startswith(ROWS_MAY_DROP) for c in fix.caveats), fix.caveats
    assert {r[0] for r in rows(db, fix.after_sql)} == kept
    assert len(rows(db, fix.after_sql)) < len(rows(db, sql))


PRESERVING = [
    "SUM(e.amount)",
    "AVG(e.amount * 2)",
    "MIN(e.amount)",
    "MAX(e.event_ts)",
    "COUNT(e.event_id)",
    "COUNT(DISTINCT e.event_id)",
    "STDDEV_SAMP(e.amount)",
    "MEDIAN(e.amount)",
    "SUM(e.amount) FILTER (WHERE e.amount > 1)",
    "MAX(l.churned)",
    "COUNT(DISTINCT l.user_id)",
]
NOT_PRESERVING = [
    "COUNT(COALESCE(e.amount, 0))",
    "COUNT_IF(e.amount IS NULL)",
    "SUM(CASE WHEN e.event_id IS NULL THEN 1 ELSE 1 END)",
    "AVG(CASE WHEN e.amount IS NULL THEN 1 ELSE 0 END)",
    "SUM(l.user_id)",
    "AVG(l.churned)",
    "SUM(COALESCE(e.amount, 0))",
    "MAX(COALESCE(e.amount, 0))",
    "LIST(e.amount)",
    "SUM(1)",
]


@pytest.mark.parametrize("agg", PRESERVING)
def test_aggregates_that_are_null_for_an_unmatched_row_keep_the_meaning(agg):
    fix = last(f"{SEL}, {agg} x {LJ} {G}")
    assert fix.semantics is Semantics.PRESERVED and fix.caveats == []


@pytest.mark.parametrize("agg", NOT_PRESERVING)
def test_aggregates_that_give_an_unmatched_row_a_value_are_not_preserved(agg):
    fix = last(f"{SEL}, {agg} x {LJ} {G}")
    assert fix.semantics is Semantics.CHANGED
    assert any("not known to equal" in c for c in fix.caveats), fix.caveats


def test_a_column_of_the_joined_table_outside_an_aggregate_changes_the_rows():
    fix = last(f"{SEL}, e.amount {LJ}")
    assert fix.semantics is Semantics.CHANGED
    assert any("outside an aggregate" in c for c in fix.caveats)


def test_a_subquery_in_the_projection_keeps_the_meaning(db):
    sql = f"{SEL}, (SELECT COUNT(*) FROM events e WHERE e.user_id = l.user_id) n FROM labels l"
    fix = last(sql)
    assert fix.semantics is Semantics.PRESERVED and fix.caveats == []
    assert dict((r[0], r[3]) for r in rows(db, fix.after_sql)) == {1: 2, 2: 0, 3: 0, 4: 1}


def test_tightening_a_bound_in_a_where_says_rows_may_drop():
    fix = last(f"{SEL}, SUM(e.amount) a {LJ} WHERE e.ingested_at <= l.label_ts {G}")
    assert fix.semantics is Semantics.CHANGED
    assert any(c.startswith(ROWS_MAY_DROP) for c in fix.caveats)


def test_an_asof_direction_flip_is_not_described_as_a_window():
    fix = last(
        f"{SEL}, s.total_spend FROM labels l "
        "ASOF LEFT JOIN user_stats s ON l.user_id = s.user_id AND l.label_ts < s.updated_at"
    )
    text = " ".join(fix.caveats)
    assert "ASOF match now picks the latest row before the prediction time" in text
    assert "mirrored" not in text
    assert fix.semantics is Semantics.CHANGED


def test_a_bound_tightened_inside_an_inner_join_condition_says_rows_may_drop():
    """The validity range of a version includes its start instant: making it strict removes the
    version that started exactly at the prediction time, and the INNER JOIN drops the row."""
    conn = duckdb.connect()
    conn.execute(
        "CREATE TABLE labels AS "
        "SELECT 1 AS user_id, TIMESTAMP '2026-02-01' AS label_ts, 0 AS churned"
    )
    conn.execute(
        "CREATE TABLE user_stats AS SELECT * FROM (VALUES "
        "(1, TIMESTAMP '2026-01-01', 1.0, 1), (1, TIMESTAMP '2026-02-01', 2.0, 2)) "
        "t(user_id, updated_at, total_spend, n_orders)"
    )
    sql = (
        "WITH versions AS (SELECT user_id, total_spend, updated_at AS valid_from, "
        "LEAD(updated_at) OVER (PARTITION BY user_id ORDER BY updated_at) AS valid_to "
        "FROM user_stats) "
        f"{SEL}, v.total_spend AS feat FROM labels l JOIN versions v ON v.user_id = l.user_id "
        "AND l.label_ts >= v.valid_from AND (l.label_ts < v.valid_to OR v.valid_to IS NULL)"
    )
    fix = last(sql)
    assert fix.semantics is Semantics.CHANGED
    assert any(c.startswith(ROWS_MAY_DROP) for c in fix.caveats), fix.caveats
    assert len(rows(conn, sql)) == 1 and len(rows(conn, fix.after_sql)) == 0


# --- what the bounded table feeds, followed up to the top of the query -------------------------

AGG = (
    "SELECT l.user_id, l.label_ts, l.churned, SUM(e.amount) a "
    "FROM labels l LEFT JOIN events e ON e.user_id = l.user_id GROUP BY 1, 2, 3"
)
SPINE = "SELECT l.user_id, l.label_ts, l.churned, NULL FROM labels l WHERE 1 = 0"


def key_counts_of(db, sql: str) -> dict:
    return dict(Counter(db.sql(f"SELECT user_id, label_ts FROM ({sql})").fetchall()))


UPWARD = {
    "derived table filtered on the aggregate": (
        f"SELECT user_id, label_ts, churned, a FROM ({AGG}) t WHERE a > 0"
    ),
    "cte filtered on IS NOT NULL": f"WITH t AS ({AGG}) SELECT * FROM t WHERE a IS NOT NULL",
    "cte joined back on the aggregate": (
        f"WITH t AS ({AGG}) SELECT l.user_id, l.label_ts, l.churned, t.a FROM labels l "
        "JOIN t ON t.user_id = l.user_id AND t.label_ts = l.label_ts AND t.a > 0"
    ),
    "filter inside a union branch": (f"SELECT * FROM ({AGG}) t WHERE t.a > 0 UNION ALL {SPINE}"),
    "two levels of derived tables": (
        f"SELECT * FROM (SELECT * FROM (SELECT user_id, label_ts, churned, a FROM ({AGG}) t1) t2) "
        "t3 WHERE a > 0"
    ),
    "renamed on the way up": (
        f"SELECT * FROM (SELECT user_id, label_ts, churned, a AS b FROM ({AGG}) t1) t2 WHERE b > 0"
    ),
    "computed on the way up, two ctes": (
        f"WITH t AS ({AGG}), u AS (SELECT user_id, label_ts, churned, a * 2 AS b FROM t) "
        "SELECT * FROM u WHERE b > 0"
    ),
    "limit over the aggregate one level out": (f"SELECT * FROM ({AGG}) t ORDER BY a DESC LIMIT 2"),
    "order by the aggregate with a limit": (
        f"{SEL}, SUM(e.amount) a {LJ} {G} ORDER BY MAX(e.amount) DESC LIMIT 2"
    ),
    "order by a joined column with a limit, no group by": (
        f"{SEL} {LJ} ORDER BY e.amount DESC LIMIT 3"
    ),
}


@pytest.mark.parametrize("sql", UPWARD.values(), ids=UPWARD.keys())
def test_a_value_the_bound_changes_that_decides_rows_further_out_is_not_preserved(db, sql):
    fix = last(sql)  # schema only: nothing replayed
    assert fix.semantics is Semantics.CHANGED
    assert any(c.startswith(ROWS_MAY_DROP) for c in fix.caveats), fix.caveats
    assert key_counts_of(db, sql) != key_counts_of(db, fix.after_sql)  # the rows really change


POSSIBLE = {
    "distinct over the aggregate one level out": (
        f"SELECT DISTINCT user_id, label_ts, churned, a FROM ({AGG}) t"
    ),
    "distinct star over the aggregate": f"SELECT DISTINCT * FROM ({AGG}) t",
    "union that removes duplicates": f"SELECT * FROM ({AGG}) t UNION {SPINE}",
    "union that removes duplicates, in a cte": (
        f"WITH t AS ({AGG}), u AS (SELECT * FROM t UNION {SPINE}) SELECT * FROM u"
    ),
}


@pytest.mark.parametrize("sql", POSSIBLE.values(), ids=POSSIBLE.keys())
def test_a_deduplication_over_the_bounded_values_says_rows_may_drop(sql):
    fix = last(sql)
    assert fix.semantics is Semantics.CHANGED
    assert any(c.startswith(ROWS_MAY_DROP) for c in fix.caveats), fix.caveats


PLAIN_UP = {
    "derived table, nothing done with it": f"SELECT * FROM ({AGG}) t",
    "two levels, plain outputs and arithmetic": (
        f"SELECT * FROM (SELECT user_id, label_ts, churned, a * 2 AS b FROM "
        f"(SELECT * FROM ({AGG}) t1) t2) t3"
    ),
    "cte read once": f"WITH t AS ({AGG}) SELECT user_id, label_ts, churned, a FROM t",
    "union all of the aggregate": f"SELECT * FROM ({AGG}) t UNION ALL {SPINE}",
    "order by the aggregate without a limit": f"{SEL}, SUM(e.amount) a {LJ} {G} ORDER BY a DESC",
}


@pytest.mark.parametrize("sql", PLAIN_UP.values(), ids=PLAIN_UP.keys())
def test_a_value_that_only_travels_up_as_an_output_keeps_the_meaning(db, sql):
    fix = last(sql)
    assert fix.semantics is Semantics.PRESERVED and fix.caveats == []
    assert key_counts_of(db, sql) == key_counts_of(db, fix.after_sql)


def test_a_filter_on_an_unrelated_column_of_the_same_scope_is_not_blamed(db):
    sql = f"SELECT * FROM ({AGG}) t WHERE churned = 0"
    assert last(sql).semantics is Semantics.PRESERVED


def test_a_table_joined_twice_names_the_fan_out_not_a_value_of_its_own():
    fixes = [
        f
        for f in fixes_of(
            f"{SEL}, SUM(e.amount) a, COUNT(e2.event_id) n FROM labels l "
            "LEFT JOIN events e ON e.user_id = l.user_id "
            f"LEFT JOIN events e2 ON e2.user_id = l.user_id {G}"
        )
        if f.rewrites
    ]
    text = " ".join(c for f in fixes for c in f.caveats)
    assert all(f.semantics is Semantics.CHANGED for f in fixes)
    assert "joined again as e2" in text and "multiplied by the matches" in text
    assert "value of its own" not in text


def test_a_frame_changed_under_distinct_says_the_row_count_changes(db):
    sql = (
        "WITH cur AS (SELECT DISTINCT user_id, LAST_VALUE(total_spend) OVER (PARTITION BY user_id "
        "ORDER BY updated_at ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) "
        "AS cur_spend FROM user_stats) "
        "SELECT lab.user_id, lab.label_ts, lab.churned, c.cur_spend FROM labels lab "
        "LEFT JOIN cur c USING (user_id)"
    )
    fix = [f for f in fixes_of(sql) if f.rewrites][0]
    assert fix.semantics is Semantics.CHANGED
    assert any(c.startswith(ROWS_CHANGE) for c in fix.caveats), fix.caveats
    assert key_counts_of(db, sql) != key_counts_of(db, fix.after_sql)  # user 1 comes back 5 times


def test_removing_the_latest_prediction_filter_has_a_row_caveat_with_a_prefix():
    fix = last(
        f"{SEL}, SUM(e.amount) a FROM labels l LEFT JOIN events e ON e.user_id = l.user_id "
        "AND e.ingested_at < l.label_ts GROUP BY 1, 2, 3 "
        "QUALIFY ROW_NUMBER() OVER (PARTITION BY l.user_id ORDER BY l.label_ts DESC) = 1"
    )
    assert any(c.startswith(ROWS_CHANGE) for c in fix.caveats)
