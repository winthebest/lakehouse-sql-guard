"""suggest_fix: every rewrite removes its finding, says what else it changes, and is re-checked.

Queries are judged by the same validator that flagged them: a rewrite is only offered as verified
when the validator no longer reports the finding and reports nothing new."""

import re

import pytest

from sqlguard.leakage import rewrite
from sqlguard.leakage.fixes import Fix, Verification, suggest_fix
from sqlguard.leakage.spec import LeakageSpec
from sqlguard.leakage.validator import static_findings, validate

from .conftest import SCHEMA, SPEC

SEL = "SELECT l.user_id, l.label_ts, l.churned"
GROUP = "GROUP BY 1, 2, 3"
LEFT = "FROM labels l LEFT JOIN events e ON e.user_id = l.user_id"
INNER = "FROM labels l JOIN events e ON e.user_id = l.user_id"
LATEST_STATS = (
    "(SELECT * FROM user_stats QUALIFY row_number() OVER "
    "(PARTITION BY user_id ORDER BY updated_at DESC) = 1)"
)
LATEST_EVENTS = (
    "(SELECT * FROM events QUALIFY row_number() OVER "
    "(PARTITION BY user_id ORDER BY event_ts DESC) = 1)"
)


def flat(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).replace("( ", "(").replace(" )", ")").strip()


def spec_with(**overrides) -> LeakageSpec:
    return LeakageSpec.from_dict({**SPEC, **overrides})


def fixes_of(sql: str, **overrides) -> list[Fix]:
    return suggest_fix(sql, spec_with(**overrides), schema=SCHEMA)


def last_rewrite(sql: str, **overrides) -> Fix:
    rewrites = [f for f in fixes_of(sql, **overrides) if f.rewrites]
    assert rewrites, "no rewrite was offered"
    return rewrites[-1]


# --- every repairable shape: finding gone, nothing new, the expected edit --------------------

REPAIRS = {
    "unbounded aggregate bounds both time columns in the join": (
        f"{SEL}, SUM(e.amount) a {LEFT} {GROUP}",
        "L3",
        ["LEFT JOIN events AS e ON", "e.event_ts < l.label_ts", "e.ingested_at < l.label_ts"],
    ),
    "table without an availability column gets one bound": (
        f"{SEL}, s.total_spend FROM labels l LEFT JOIN user_stats s ON s.user_id = l.user_id",
        "L2",
        ["s.updated_at < l.label_ts"],
    ),
    "inner join becomes left join and count star counts the joined side": (
        f"{SEL}, COUNT(*) n {INNER} {GROUP}",
        "L3",
        ["LEFT JOIN events AS e", "COUNT(e.user_id)"],
    ),
    "correlated subquery gets the bound in its own where": (
        f"{SEL}, (SELECT SUM(e.amount) FROM events e WHERE e.user_id = l.user_id) a FROM labels l",
        "L3",
        ["WHERE e.user_id = l.user_id AND e.event_ts < l.label_ts"],
    ),
    "comma join gets the bound in the where": (
        f"{SEL}, SUM(e.amount) a FROM labels l, events e WHERE e.user_id = l.user_id {GROUP}",
        "L3",
        ["e.event_ts < l.label_ts AND e.ingested_at < l.label_ts"],
    ),
    "spine that is a derived table": (
        "SELECT sp.user_id, sp.label_ts, sp.churned, SUM(e.amount) a FROM "
        "(SELECT user_id, label_ts, churned FROM labels WHERE user_id < 100 "
        "UNION ALL SELECT user_id, label_ts, churned FROM labels WHERE user_id >= 100) sp "
        "LEFT JOIN events e ON e.user_id = sp.user_id GROUP BY 1, 2, 3",
        "L3",
        ["e.event_ts < sp.label_ts", "e.ingested_at < sp.label_ts"],
    ),
    "event time only gets the availability bound": (
        f"{SEL}, SUM(e.amount) a FROM labels l LEFT JOIN events e ON e.user_id = l.user_id "
        f"AND e.event_ts < l.label_ts {GROUP}",
        "L4",
        ["e.event_ts < l.label_ts", "e.ingested_at < l.label_ts"],
    ),
    "latest record is ordered by availability after event time": (
        f"{SEL}, (SELECT e.amount FROM events e WHERE e.user_id = l.user_id "
        "AND e.event_ts < l.label_ts ORDER BY e.event_ts DESC LIMIT 1) last FROM labels l",
        "L4",
        ["ORDER BY e.event_ts DESC, e.ingested_at DESC"],
    ),
    "inclusive bound becomes strict": (
        f"{SEL}, SUM(e.amount) a FROM labels l LEFT JOIN events e ON e.user_id = l.user_id "
        f"AND e.ingested_at <= l.label_ts {GROUP}",
        "L5",
        ["e.ingested_at < l.label_ts"],
    ),
    "epoch spelling of an inclusive bound": (
        f"{SEL}, SUM(e.amount) a FROM labels l LEFT JOIN events e ON e.user_id = l.user_id "
        f"AND epoch(e.ingested_at) <= epoch(l.label_ts) {GROUP}",
        "L5",
        ["e.ingested_at < l.label_ts"],
    ),
    "timezone shift is dropped": (
        f"{SEL}, SUM(e.amount) a FROM labels l LEFT JOIN events e ON e.user_id = l.user_id "
        f"AND e.ingested_at < l.label_ts + INTERVAL 7 HOUR {GROUP}",
        "L5",
        ["e.ingested_at < l.label_ts"],
    ),
    "inclusive end of a between": (
        f"{SEL}, SUM(e.amount) a FROM labels l LEFT JOIN events e ON e.user_id = l.user_id "
        f"AND e.ingested_at BETWEEN l.label_ts - INTERVAL 7 DAY AND l.label_ts {GROUP}",
        "L5",
        ["e.ingested_at >= l.label_ts - INTERVAL '7' DAY", "e.ingested_at < l.label_ts"],
    ),
    "forward window is mirrored into the past": (
        f"{SEL}, SUM(e.amount) a FROM labels l LEFT JOIN events e ON e.user_id = l.user_id "
        f"AND e.ingested_at >= l.label_ts AND e.ingested_at < l.label_ts + INTERVAL 14 DAY {GROUP}",
        "L1",
        ["e.ingested_at >= l.label_ts - INTERVAL '14' DAY", "e.ingested_at < l.label_ts"],
    ),
    "reversed spelling of a forward window": (
        f"{SEL}, SUM(e.amount) a FROM labels l LEFT JOIN events e ON e.user_id = l.user_id "
        f"AND date_diff('second', l.label_ts, e.ingested_at) > 0 "
        f"AND e.ingested_at < l.label_ts + INTERVAL 14 DAY {GROUP}",
        "L1",
        ["e.ingested_at < l.label_ts"],
    ),
    "asof join that looks forward": (
        f"{SEL}, s.total_spend FROM labels l "
        "ASOF LEFT JOIN user_stats s ON l.user_id = s.user_id AND l.label_ts < s.updated_at",
        "L1",
        ["ASOF LEFT JOIN user_stats AS s", "l.label_ts > s.updated_at"],
    ),
    "asof join with an inclusive bound": (
        f"{SEL}, s.total_spend FROM labels l "
        "ASOF JOIN user_stats s ON l.user_id = s.user_id AND l.label_ts >= s.updated_at",
        "L5",
        ["l.label_ts > s.updated_at"],
    ),
    "first snapshot after becomes last snapshot before": (
        f"{SEL}, s.n_orders FROM labels l JOIN user_stats s ON s.user_id = l.user_id "
        "AND s.updated_at > l.label_ts + INTERVAL 3 DAY "
        "QUALIFY row_number() OVER (PARTITION BY l.user_id, l.label_ts ORDER BY s.updated_at) = 1",
        "L1",
        ["s.updated_at < l.label_ts - INTERVAL '3' DAY", "ORDER BY s.updated_at DESC"],
    ),
    "next value subqueries read the previous one": (
        f"{SEL}, (SELECT min_by(st.n_orders, st.updated_at) FROM user_stats st "
        "WHERE st.user_id = l.user_id AND st.updated_at > l.label_ts) nxt FROM labels l",
        "L1",
        ["ARG_MAX(st.n_orders, st.updated_at)", "st.updated_at < l.label_ts"],
    ),
    "frame that reaches the following rows": (
        "WITH f AS (SELECT user_id, updated_at, SUM(total_spend) OVER (PARTITION BY user_id "
        "ORDER BY updated_at ROWS BETWEEN CURRENT ROW AND UNBOUNDED FOLLOWING) AS v "
        "FROM user_stats) "
        f"{SEL}, f.v FROM labels l ASOF JOIN f ON f.user_id = l.user_id "
        "AND l.label_ts > f.updated_at",
        "L1",
        ["ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING"],
    ),
    "lead as an output column is left out": (
        f"{SEL}, s.total_spend, LEAD(s.updated_at) OVER (PARTITION BY l.user_id "
        "ORDER BY s.updated_at) AS next_ts FROM labels l "
        "ASOF JOIN user_stats s ON s.user_id = l.user_id AND l.label_ts > s.updated_at",
        "L1",
        ["s.total_spend FROM labels"],
    ),
    "only each entity's latest prediction is kept": (
        f"{SEL}, SUM(e.amount) a FROM labels l LEFT JOIN events e ON e.user_id = l.user_id "
        "AND e.ingested_at < l.label_ts GROUP BY 1, 2, 3 "
        "QUALIFY ROW_NUMBER() OVER (PARTITION BY l.user_id ORDER BY l.label_ts DESC) = 1",
        "L1",
        ["GROUP BY"],
    ),
    "having max on the table's time is dropped once the join is bounded": (
        f"{SEL}, AVG(e.amount) a FROM labels l LEFT JOIN events e ON e.user_id = l.user_id "
        f"AND e.event_ts >= l.label_ts - INTERVAL 14 DAY {GROUP} "
        "HAVING MAX(e.ingested_at) < l.label_ts",
        "L3",
        ["e.event_ts < l.label_ts", "e.ingested_at < l.label_ts"],
    ),
    "latest row derived table becomes an asof join": (
        f"{SEL}, s.total_spend FROM labels l JOIN {LATEST_STATS} s ON s.user_id = l.user_id",
        "L2",
        ["ASOF LEFT JOIN user_stats AS s", "l.label_ts > s.updated_at"],
    ),
    "latest row cte used once": (
        f"WITH latest AS {LATEST_STATS} "
        f"{SEL}, lt.total_spend FROM labels l LEFT JOIN latest lt ON lt.user_id = l.user_id",
        "L2",
        ["ASOF LEFT JOIN user_stats AS lt"],
    ),
    "latest row of a table with two time columns is ranked, never asof": (
        f"{SEL}, s.amount FROM labels l LEFT JOIN {LATEST_EVENTS} s ON s.user_id = l.user_id",
        "L2",
        [
            "s.event_ts < l.label_ts AND s.ingested_at < l.label_ts",
            "PARTITION BY l.user_id, l.label_ts ORDER BY s.event_ts DESC, s.ingested_at DESC",
        ],
    ),
    "latest row correlated pick": (
        f"{SEL}, s.total_spend FROM labels l JOIN user_stats s ON s.user_id = l.user_id "
        "WHERE s.updated_at IN (SELECT MAX(x.updated_at) FROM user_stats x "
        "WHERE x.user_id = l.user_id)",
        "L2",
        ["x.updated_at < l.label_ts"],
    ),
}


@pytest.mark.parametrize("sql, rule, snippets", REPAIRS.values(), ids=REPAIRS.keys())
def test_repair_is_verified_statically_and_makes_the_expected_edit(sql, rule, snippets):
    report = validate(sql, spec_with(), schema=SCHEMA)
    assert rule in {f.rule_id for f in report.findings}
    fixes = fixes_of(sql)
    fix = fixes[-1] if fixes[-1].rewrites else last_rewrite(sql)
    assert fix.verification is Verification.STATIC_ONLY
    assert static_findings(fix.after_sql, spec_with(), SCHEMA) == []
    text = flat(fix.after_sql)
    for snippet in snippets:
        assert flat(snippet) in text, (snippet, text)
    assert not any(f.verification is Verification.UNVERIFIED for f in fixes if f.rewrites)


def test_every_rewrite_chain_starts_from_the_query_it_was_given():
    sql = f"{SEL}, SUM(e.amount) a {LEFT} AND e.event_ts >= l.label_ts {GROUP}"
    fixes = [f for f in fixes_of(sql) if f.rewrites]
    assert len(fixes) >= 2  # the forward window, then the availability bound
    for earlier, later in zip(fixes, fixes[1:], strict=False):
        assert earlier.after_sql == later.before_sql
    assert "e.event_ts >= l.label_ts" not in flat(fixes[-1].after_sql)


# --- what the user is told ---------------------------------------------------------------------


def test_inner_join_to_left_join_is_a_caveat_and_keeps_the_spine():
    fix = last_rewrite(f"{SEL}, COUNT(*) n {INNER} {GROUP}")
    text = " ".join(fix.caveats)
    assert "INNER JOIN became LEFT JOIN" in text and "COUNT(*) became COUNT(e.user_id)" in text


def test_a_bound_in_the_where_of_an_inner_join_says_that_rows_can_drop():
    fix = last_rewrite(
        f"{SEL}, SUM(e.amount) a FROM labels l, events e WHERE e.user_id = l.user_id {GROUP}"
    )
    assert any("drop out" in c for c in fix.caveats)


def test_a_mirrored_window_asks_the_author_to_confirm():
    fix = last_rewrite(
        f"{SEL}, SUM(e.amount) a {LEFT} AND e.ingested_at >= l.label_ts "
        f"AND e.ingested_at < l.label_ts + INTERVAL 7 DAY {GROUP}"
    )
    assert any("mirrored" in c for c in fix.caveats)


def test_removing_the_latest_prediction_filter_says_what_the_result_becomes():
    fix = last_rewrite(
        f"{SEL}, SUM(e.amount) a {LEFT} AND e.ingested_at < l.label_ts {GROUP} "
        "QUALIFY ROW_NUMBER() OVER (PARTITION BY l.user_id ORDER BY l.label_ts DESC) = 1"
    )
    assert any("one row per prediction time" in c for c in fix.caveats)


# --- shapes that are explained, not rewritten --------------------------------------------------

EXPLAINED = {
    "aggregate cte grouped by entity only": (
        "WITH t AS (SELECT user_id, SUM(amount) AS total FROM events GROUP BY user_id) "
        f"{SEL}, t.total FROM labels l LEFT JOIN t ON t.user_id = l.user_id",
        "L3",
    ),
    "feature from the outcome table": (
        f"{SEL}, SUM(r.refund_after_churn) x FROM labels l LEFT JOIN user_outcomes r "
        f"ON r.user_id = l.user_id {GROUP}",
        "L6",
    ),
    "target encoding of the label": (
        f"{SEL}, AVG(b.churned) te FROM labels l "
        f"LEFT JOIN labels b ON b.user_id = l.user_id {GROUP}",
        "L6",
    ),
    "wall clock in a feature": (
        f"{SEL}, date_diff('day', MAX(e.event_ts), now()) recency {LEFT} "
        f"AND e.ingested_at < l.label_ts {GROUP}",
        "L7",
    ),
    "timezone mismatch needs a cast the author must choose": (
        f"{SEL}, COUNT(c.page) n FROM labels l LEFT JOIN clicks c ON c.user_id = l.user_id "
        f"AND c.click_ts < l.label_ts {GROUP}",
        "L5",
    ),
    "label table aggregated per entity": (
        f"{SEL}, SUM(e.amount) a {LEFT} AND e.ingested_at < l.label_ts "
        "WHERE l.label_ts < (SELECT MAX(x.label_ts) FROM labels x WHERE x.user_id = l.user_id) "
        f"{GROUP}",
        "L3",
    ),
}


@pytest.mark.parametrize("sql, rule", EXPLAINED.values(), ids=EXPLAINED.keys())
def test_unrewritable_findings_are_explained_and_marked_unverified(sql, rule):
    fixes = fixes_of(sql)
    explained = [f for f in fixes if f.rule == rule and not f.rewrites]
    assert explained, [(f.rule, f.rewrites) for f in fixes]
    for fix in explained:
        assert fix.after_sql is None and fix.explanation
        assert fix.verification is Verification.UNVERIFIED
        assert fix.before_sql


def test_nothing_is_offered_for_a_clean_query():
    sql = (
        f"{SEL}, SUM(e.amount) a {LEFT} AND e.ingested_at < l.label_ts "
        f"AND e.event_ts >= l.label_ts - INTERVAL 7 DAY {GROUP}"
    )
    assert fixes_of(sql) == []


def test_nothing_is_offered_for_a_query_that_does_not_parse_or_is_not_a_select():
    assert fixes_of("SELEC nonsense") == []
    assert fixes_of("DELETE FROM events") == []


def test_one_fixable_and_one_unfixable_leak_give_a_rewrite_and_an_explanation():
    sql = (
        f"{SEL}, SUM(e.amount) a, SUM(r.refund_after_churn) x FROM labels l "
        "LEFT JOIN events e ON e.user_id = l.user_id "
        f"LEFT JOIN user_outcomes r ON r.user_id = l.user_id {GROUP}"
    )
    fixes = fixes_of(sql)
    assert {(f.rule, f.rewrites) for f in fixes} >= {("L3", True), ("L6", False)}
    rewrite_fix = next(f for f in fixes if f.rewrites)
    # the L6 leak is still there, so this rewrite is not certified as a whole
    assert rewrite_fix.verification is Verification.STATIC_ONLY


# --- ASOF: one time column only, written per dialect -------------------------------------------


@pytest.mark.parametrize("dialect", ["duckdb", "snowflake", "spark", "trino"])
def test_a_table_with_two_time_columns_never_gets_an_asof_join(dialect):
    """events has an event time and an availability time: one inequality cannot hold both."""
    sql = (
        f"{SEL}, s.amount FROM labels l LEFT JOIN {LATEST_EVENTS} s ON s.user_id = l.user_id"
        if dialect in ("duckdb", "snowflake")
        else f"{SEL}, SUM(e.amount) a {LEFT} {GROUP}"
    )
    fixes = fixes_of(sql, dialect=dialect)
    assert any(f.rewrites for f in fixes)
    for fix in fixes:
        assert "ASOF" not in (fix.after_sql or "").upper()


def test_a_table_with_two_time_columns_is_never_rewritten_into_an_existing_asof_join():
    sql = (
        f"{SEL}, e.amount FROM labels l "
        "ASOF LEFT JOIN events e ON e.user_id = l.user_id AND l.label_ts > e.event_ts"
    )
    fixes = fixes_of(sql)
    assert not any(f.rewrites for f in fixes)  # the availability bound cannot join an ASOF
    assert any(f.rule == "L4" for f in fixes)


def test_duckdb_gets_an_asof_left_join_for_a_single_time_column():
    fix = last_rewrite(
        f"{SEL}, s.total_spend FROM labels l JOIN {LATEST_STATS} s ON s.user_id = l.user_id"
    )
    assert "ASOF LEFT JOIN user_stats AS s" in fix.after_sql
    assert "l.label_ts > s.updated_at" in fix.after_sql


def test_snowflake_asof_is_written_with_match_condition():
    fix = last_rewrite(
        f"{SEL}, s.total_spend FROM labels l LEFT JOIN {LATEST_STATS} s ON s.user_id = l.user_id",
        dialect="snowflake",
    )
    assert flat(fix.after_sql).endswith(
        "FROM labels AS l ASOF JOIN user_stats AS s "
        "MATCH_CONDITION (l.label_ts > s.updated_at) ON s.user_id = l.user_id"
    )
    assert "ASOF LEFT" not in fix.after_sql  # Snowflake has no LEFT form


def test_snowflake_match_condition_in_the_input_is_understood():
    sql = (
        f"{SEL}, s.total_spend FROM labels l "
        "ASOF JOIN user_stats s MATCH_CONDITION (l.label_ts <= s.updated_at) "
        "ON s.user_id = l.user_id"
    )
    fix = last_rewrite(sql, dialect="snowflake")
    assert "MATCH_CONDITION (l.label_ts > s.updated_at)" in flat(fix.after_sql)


@pytest.mark.parametrize("dialect", ["spark", "trino"])
def test_dialects_without_asof_rank_the_latest_row(dialect):
    fix = last_rewrite(f"{SEL}, SUM(e.amount) a {LEFT} {GROUP}", dialect=dialect)
    assert "ASOF" not in fix.after_sql


def test_spark_latest_row_is_a_row_number_filter():
    fix = last_rewrite(
        f"{SEL}, s.total_spend FROM labels l LEFT JOIN {LATEST_STATS} s ON s.user_id = l.user_id",
        dialect="spark",
    )
    text = fix.after_sql
    assert "ASOF" not in text and "ROW_NUMBER() OVER" in text and "= 1" in text


def test_asof_condition_builder_marks_the_one_inequality():
    spine, column = rewrite.exp.column("label_ts", table="l"), rewrite.exp.column("t", table="s")
    condition = rewrite.asof_condition(spine, column)
    assert condition.sql() == "l.label_ts > s.t" and condition.meta[rewrite.ASOF_MARK]


# --- the checker is the judge ------------------------------------------------------------------


def test_an_edit_the_checker_does_not_accept_is_unverified(monkeypatch):
    """A bound on a column the checker does not read as a time bound leaves the finding."""

    def wrong(ctx, ref, rule):
        table = ctx.orig(ref.node)
        spine = ctx.spine_column(ref)
        select, join = rewrite._select_of(table)
        join.set(
            "on",
            rewrite.conjoin(
                join.args["on"],
                rewrite.before(rewrite.exp.column("amount", table=table.alias_or_name), spine),
            ),
        )
        return rewrite.Edit(rule, ref.describe(), "a bound on the wrong column")

    monkeypatch.setattr(rewrite, "add_bounds", wrong)
    fixes = fixes_of(f"{SEL}, SUM(e.amount) a {LEFT} {GROUP}")
    rewrites = [f for f in fixes if f.rewrites]
    assert [f.verification for f in rewrites] == [Verification.UNVERIFIED]
    assert any("still reports L3" in c for c in rewrites[0].caveats)


def test_an_edit_that_breaks_the_query_is_never_offered(monkeypatch):
    def broken(ctx, ref, rule):
        table = ctx.orig(ref.node)
        select, join = rewrite._select_of(table)
        join.set("on", rewrite.exp.column("label_ts", table=""))
        return rewrite.Edit(rule, ref.describe(), "broken")

    monkeypatch.setattr(rewrite, "add_bounds", broken)
    assert not any(f.rewrites for f in fixes_of(f"{SEL}, SUM(e.amount) a {LEFT} {GROUP}"))


def test_a_bug_in_a_rewrite_never_breaks_validation(monkeypatch):
    def boom(ctx, ref, rule):
        raise RuntimeError("bug")

    monkeypatch.setattr(rewrite, "add_bounds", boom)
    sql = f"{SEL}, SUM(e.amount) a {LEFT} {GROUP}"
    fixes = fixes_of(sql)
    assert fixes and not any(f.rewrites for f in fixes)
    assert validate(sql, spec_with(), schema=SCHEMA).verdict == "leaky"


def test_suggesting_fixes_changes_nothing_about_the_verdict_or_the_inputs():
    sql = f"{SEL}, SUM(e.amount) a {LEFT} {GROUP}"
    spec = spec_with()
    before = validate(sql, spec, schema=SCHEMA).to_dict()
    suggest_fix(sql, spec, schema=SCHEMA)
    assert validate(sql, spec, schema=SCHEMA).to_dict() == before
    assert spec == spec_with()


def test_fix_serializes_for_json():
    fix = last_rewrite(f"{SEL}, SUM(e.amount) a {LEFT} {GROUP}")
    data = fix.to_dict()
    assert data["verification"] == "static_only" and data["rule"] == "L3"
    assert set(data) == {
        "rule",
        "location",
        "before_sql",
        "after_sql",
        "explanation",
        "caveats",
        "verification",
    }


def test_suggest_fix_needs_a_schema_or_a_catalog():
    with pytest.raises(ValueError, match="schema or a catalog"):
        suggest_fix("SELECT 1", spec_with())


def test_the_analysis_of_a_parsed_tree_leaves_the_tree_alone():
    from sqlguard.core.parsing import parse_single
    from sqlguard.leakage.analysis import analyze_tree

    sql = (
        "WITH ev AS (SELECT user_id, ingested_at AS t, amount FROM events UNION ALL BY NAME "
        "SELECT user_id, ingested_at AS t, amount FROM events) "
        f"{SEL}, SUM(ev.amount) a FROM labels l LEFT JOIN ev ON ev.user_id = l.user_id "
        f"AND ev.t < l.label_ts {GROUP}"
    )
    tree = parse_single(sql, "duckdb")
    before = tree.sql("duckdb")
    analyze_tree(tree, spec_with(), SCHEMA)
    assert tree.sql("duckdb") == before


def test_the_dialect_argument_is_the_dialect_of_the_query():
    sql = f"{SEL}, SUM(e.amount) a {LEFT} {GROUP}"
    fixes = suggest_fix(sql, spec_with(dialect="duckdb"), None, "spark", schema=SCHEMA)
    assert fixes and fixes[-1].rewrites
