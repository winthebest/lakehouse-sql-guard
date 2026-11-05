"""Shapes the second rule set misjudged on queries it had not seen (see docs/limitations.md): a
bound or window that the analysis did not trace through a CTE, a surrogate key, a calendar
scaffold, a merged timeline or a named window; and peers whose labels are final. Every exemption
is paired with a query of the same shape and one wrong condition, which must stay flagged."""

import pytest

from sqlguard.core.lineage import SourceColumn, full_lineage
from sqlguard.leakage.analysis import MATURED_LABELS, analyze
from sqlguard.leakage.spec import LeakageSpec
from sqlguard.leakage.validator import validate

from .conftest import SCHEMA, SPEC

SEL = "SELECT l.user_id, l.label_ts, l.churned"
RELATIONS = {
    "analytics.marts.fct_training_spine": "labels",
    "analytics.staging.stg_events": "events",
    "analytics.staging.stg_user_stats": "user_stats",
}


def check(sql: str, **spec_overrides) -> tuple[str, set[str]]:
    spec = LeakageSpec.from_dict({**SPEC, **spec_overrides})
    report = validate(sql, spec, schema=SCHEMA)
    return report.verdict, {f.rule_id for f in report.findings}


# --- a window over rows cut upstream (L1) ---------------------------------------------------

PICK = """WITH s AS (SELECT user_id, updated_at, total_spend FROM user_stats)
SELECT l.user_id, l.label_ts, l.churned, s.total_spend
FROM labels l JOIN s ON s.user_id = l.user_id AND s.updated_at {op} l.label_ts
QUALIFY row_number() OVER (PARTITION BY {part} ORDER BY s.updated_at DESC) = 1"""

# the newest row of a table that is cut in the scope of the window, or in a CTE it reads, is the
# newest row before prediction time
WINDOW_CUT = {
    "point-in-time pick through a staging cte": PICK.format(op="<", part="l.user_id, l.label_ts"),
    "partitioned by the prediction time alone": PICK.format(op="<", part="l.label_ts"),
    "trailing average computed before the pick": """WITH sm AS (
  SELECT user_id, updated_at, AVG(total_spend) OVER (PARTITION BY user_id ORDER BY updated_at
    ROWS BETWEEN 3 PRECEDING AND CURRENT ROW) AS ma FROM user_stats)
SELECT l.user_id, l.label_ts, l.churned, sm.ma
FROM labels l LEFT JOIN sm ON sm.user_id = l.user_id AND sm.updated_at < l.label_ts
QUALIFY row_number() OVER (PARTITION BY l.user_id, l.label_ts ORDER BY sm.updated_at DESC) = 1""",
    "rank inside one snapshot, snapshot picked before": """WITH r AS (
  SELECT user_id, updated_at, percent_rank() OVER (PARTITION BY updated_at ORDER BY total_spend)
    AS pct FROM user_stats)
SELECT l.user_id, l.label_ts, l.churned, r.pct
FROM labels l LEFT JOIN r ON r.user_id = l.user_id AND r.updated_at < l.label_ts
QUALIFY row_number() OVER (PARTITION BY l.user_id, l.label_ts ORDER BY r.updated_at DESC) = 1""",
}

WINDOW_NOT_CUT = {
    "partition mixes predictions": (PICK.format(op="<", part="l.user_id"), "L1"),
    "inclusive cut": (PICK.format(op="<=", part="l.user_id, l.label_ts"), "L5"),
    "cut reaches into the future": (
        PICK.format(op="<", part="l.user_id, l.label_ts").replace(
            "s.updated_at < l.label_ts", "s.updated_at < l.label_ts + INTERVAL 2 DAY"
        ),
        "L1",
    ),
    "no cut at all": (
        PICK.format(op="<", part="l.user_id, l.label_ts").replace(
            " AND s.updated_at < l.label_ts", ""
        ),
        "L2",
    ),
    "cut only after the window ran": (
        """WITH r AS (
  SELECT user_id, updated_at, total_spend,
    row_number() OVER (PARTITION BY user_id ORDER BY updated_at DESC) AS rn FROM user_stats)
SELECT l.user_id, l.label_ts, l.churned, r.total_spend
FROM labels l JOIN r ON r.user_id = l.user_id AND r.updated_at < l.label_ts AND r.rn = 1""",
        "L1",
    ),
    "cut written in the qualify beside the window": (
        """SELECT l.user_id, l.label_ts, l.churned, s.total_spend
FROM labels l JOIN user_stats s ON s.user_id = l.user_id
QUALIFY s.updated_at < l.label_ts
  AND row_number() OVER (PARTITION BY l.user_id, l.label_ts ORDER BY s.updated_at DESC) = 1""",
        "L1",
    ),
    "trailing average over rows after the cut": (
        """WITH sm AS (
  SELECT user_id, updated_at, AVG(total_spend) OVER (PARTITION BY user_id ORDER BY updated_at
    ROWS BETWEEN 3 PRECEDING AND 3 FOLLOWING) AS ma FROM user_stats)
SELECT l.user_id, l.label_ts, l.churned, sm.ma
FROM labels l LEFT JOIN sm ON sm.user_id = l.user_id AND sm.updated_at < l.label_ts
QUALIFY row_number() OVER (PARTITION BY l.user_id, l.label_ts ORDER BY sm.updated_at DESC) = 1""",
        "L1",
    ),
}


@pytest.mark.parametrize("sql", WINDOW_CUT.values(), ids=WINDOW_CUT.keys())
def test_a_window_over_rows_cut_upstream_is_clean(sql):
    assert check(sql) == ("clean", set())


@pytest.mark.parametrize("sql, rule", WINDOW_NOT_CUT.values(), ids=WINDOW_NOT_CUT.keys())
def test_a_window_over_rows_not_cut_before_it_stays_flagged(sql, rule):
    verdict, rules = check(sql)
    assert verdict == "leaky" and rule in rules, (verdict, rules)


# --- a surrogate key of the prediction row (L1, L2) ----------------------------------------

KEYED = """WITH sp AS (
  SELECT user_id, label_ts, churned, {key} AS sk FROM analytics.marts.fct_training_spine),
pit AS (
  SELECT sp.sk, s.total_spend
  FROM sp JOIN user_stats s ON s.user_id = sp.user_id{bound}
  QUALIFY row_number() OVER (PARTITION BY {part} ORDER BY s.updated_at DESC) = 1)
SELECT sp.user_id, sp.label_ts, sp.churned, pit.total_spend
FROM sp LEFT JOIN pit ON pit.sk = sp.sk"""
KEY = "md5(CAST(user_id AS VARCHAR) || '|' || CAST(label_ts AS VARCHAR))"
BOUND = " AND s.updated_at < sp.label_ts"


def keyed(key=KEY, bound=BOUND, part="sp.sk"):
    return KEYED.format(key=key, bound=bound, part=part)


@pytest.mark.parametrize(
    "key",
    [
        KEY,
        "md5(concat(user_id, label_ts))",
        "hash(user_id, label_ts)",
        "sha256(concat_ws('|', user_id, CAST(label_ts AS TIMESTAMP)))",
        # dbt's own spelling, and the form dbt_utils.generate_surrogate_key compiles to
        "md5(COALESCE(CAST(user_id AS VARCHAR), '') || '|' "
        "|| COALESCE(CAST(label_ts AS VARCHAR), ''))",
        "md5(cast(coalesce(cast(user_id as TEXT), '_dbt_utils_surrogate_key_null_') || '-' "
        "|| coalesce(cast(label_ts as TEXT), '_dbt_utils_surrogate_key_null_') as TEXT))",
        "md5(concat_ws('-', IFNULL(CAST(user_id AS VARCHAR), 'n'), CAST(label_ts AS VARCHAR)))",
        "CAST(user_id AS VARCHAR) || '|' || CAST(label_ts AS VARCHAR)",
        "hash(user_id, label_ts)",
    ],
)
def test_a_surrogate_key_of_the_label_row_stands_for_the_prediction(key):
    assert check(keyed(key), relations=RELATIONS) == ("clean", set())


# keys whose value does not depend on both the entity and the prediction time, though the columns
# appear in them
FALSE_KEYS = {
    "coalesce between the columns": (
        "md5(COALESCE(CAST(user_id AS VARCHAR), CAST(label_ts AS VARCHAR)))"
    ),
    "column as the separator": (
        "md5(concat_ws(CAST(label_ts AS VARCHAR), CAST(user_id AS VARCHAR)))"
    ),
    "coalesce between the columns under upper": (
        "md5(UPPER(COALESCE(CAST(user_id AS VARCHAR), CAST(label_ts AS VARCHAR))))"
    ),
    "upper around valid parts": "md5(UPPER(CAST(user_id AS VARCHAR) || CAST(label_ts AS VARCHAR)))",
    "substr of the time": (
        "md5(CAST(user_id AS VARCHAR) || SUBSTR(CAST(label_ts AS VARCHAR), 1, 10))"
    ),
    "time only inside a case": (
        "md5(CAST(user_id AS VARCHAR) || "
        "CASE WHEN user_id > 0 THEN CAST(label_ts AS VARCHAR) ELSE '' END)"
    ),
    "user twice, time missing": "md5(CAST(user_id AS VARCHAR) || '|' || CAST(user_id AS VARCHAR))",
    "nullif": "md5(CAST(user_id AS VARCHAR) || NULLIF(CAST(label_ts AS VARCHAR), ''))",
    "column fallback in coalesce": (
        "md5(COALESCE(CAST(user_id AS VARCHAR), '') "
        "|| COALESCE(CAST(label_ts AS VARCHAR), churned))"
    ),
    "non-string literal part": "md5(CAST(user_id AS VARCHAR) || 7 || CAST(label_ts AS VARCHAR))",
}


@pytest.mark.parametrize("key", FALSE_KEYS.values(), ids=FALSE_KEYS.keys())
def test_a_key_that_does_not_depend_on_the_entity_and_the_time_is_no_alignment(key):
    verdict, rules = check(keyed(key=key), relations=RELATIONS)
    assert verdict == "leaky" and "L2" in rules, (verdict, rules)
    joined = DAY_KEY_SELF_JOIN.replace("md5(CAST(CAST(label_ts AS DATE) AS VARCHAR))", key)
    verdict, rules = check(joined)
    assert verdict == "leaky" and "L2" in rules, (verdict, rules)


KEYED_LEAKY = {
    "key of the prediction time alone": (
        keyed(key="sha256(CAST(label_ts AS VARCHAR))"),
        "L2",
    ),
    "key of the day": (
        keyed(key="md5(CAST(CAST(label_ts AS DATE) AS VARCHAR))"),
        "L2",
    ),
    "key of the user and the day": (
        keyed(
            key="md5(CAST(user_id AS VARCHAR) || '|' || CAST(CAST(label_ts AS DATE) AS VARCHAR))"
        ),
        "L2",
    ),
    "key of the user and the day, concat_ws": (
        keyed(key="md5(concat_ws('|', user_id, CAST(label_ts AS DATE)))"),
        "L2",
    ),
    "key with the time truncated to the hour": (
        keyed(key="md5(CAST(user_id AS VARCHAR) || CAST(date_trunc('hour', label_ts) AS VARCHAR))"),
        "L2",
    ),
    "key without the prediction time": (keyed(key="md5(CAST(user_id AS VARCHAR))"), "L2"),
    "key mixing in the target": (
        keyed(key="md5(CAST(user_id AS VARCHAR) || CAST(label_ts AS VARCHAR) || churned)"),
        "L6",
    ),
    "key that is not a hash of the row": (keyed(key="year(label_ts)"), "L2"),
    "no bound on the joined table": (keyed(bound=""), "L2"),
    "inclusive bound": (keyed(bound=BOUND.replace("<", "<=")), "L5"),
    "partition by the user only": (keyed(part="sp.user_id"), "L1"),
}


@pytest.mark.parametrize("sql, rule", KEYED_LEAKY.values(), ids=KEYED_LEAKY.keys())
def test_a_key_that_does_not_name_the_prediction_stays_flagged(sql, rule):
    verdict, rules = check(sql, relations=RELATIONS)
    assert verdict == "leaky" and rule in rules, (verdict, rules)


DBT_MART = """WITH stg_events AS (
  SELECT event_id, user_id AS customer_id, event_ts AS purchased_at, ingested_at AS loaded_at,
         amount FROM analytics.staging.stg_events
  QUALIFY row_number() OVER (PARTITION BY event_id ORDER BY ingested_at) = 1),
stg_spine AS (
  SELECT md5(CAST(user_id AS VARCHAR) || '|' || CAST(label_ts AS VARCHAR)) AS spine_key,
         user_id, label_ts, churned
  FROM analytics.marts.fct_training_spine),
int_known AS (
  SELECT s.spine_key, e.amount FROM stg_spine s JOIN stg_events e
    ON e.customer_id = s.user_id{bound} AND e.purchased_at >= s.label_ts - INTERVAL 30 DAY),
fct AS (SELECT spine_key, SUM(amount) AS spend FROM int_known GROUP BY spine_key)
SELECT s.user_id, s.label_ts, s.churned, COALESCE(f.spend, 0) AS spend
FROM stg_spine s LEFT JOIN fct f ON f.spine_key = s.spine_key"""


def test_a_dbt_mart_reading_its_own_spine_model_is_not_an_unbounded_join():
    clean = DBT_MART.format(bound=" AND e.loaded_at < s.label_ts")
    assert check(clean, relations=RELATIONS) == ("clean", set())
    verdict, rules = check(DBT_MART.format(bound=""), relations=RELATIONS)
    assert verdict == "leaky" and "L2" in rules


# --- a calendar scaffold (L2) ---------------------------------------------------------------

SCAFFOLD = """WITH sc AS (
  SELECT l.user_id, l.label_ts, l.churned,
         unnest(generate_series(date_trunc('day', l.label_ts) - INTERVAL 3 DAY,
                                date_trunc('day', l.label_ts), INTERVAL 1 DAY)) AS midnight
  FROM labels l),
daily AS (
  SELECT sc.user_id, sc.label_ts, sc.churned, s.total_spend AS v
  FROM sc LEFT JOIN user_stats s ON s.user_id = sc.user_id AND {link} {where})
SELECT user_id, label_ts, churned, MAX(v) - MIN(v) AS spend_range FROM daily GROUP BY 1, 2, 3"""
EQ = "s.updated_at = sc.midnight"


def scaffold(link=EQ, where="WHERE sc.midnight < sc.label_ts"):
    return SCAFFOLD.format(link=link, where=where)


def test_stats_equated_to_a_calendar_before_the_prediction_are_bounded():
    assert check(scaffold()) == ("clean", set())
    assert check(scaffold(where="WHERE sc.label_ts > sc.midnight")) == ("clean", set())
    assert check(scaffold(link="sc.midnight = s.updated_at")) == ("clean", set())


SCAFFOLD_LEAKY = {
    "calendar not cut": (scaffold(where=""), "L2"),
    "calendar cut inclusively": (scaffold(where="WHERE sc.midnight <= sc.label_ts"), "L5"),
    "calendar cut past the prediction": (
        scaffold(where="WHERE sc.midnight < sc.label_ts + INTERVAL 2 DAY"),
        "L1",
    ),
    "stats only ordered against the calendar": (
        scaffold(link="s.updated_at >= sc.midnight"),
        "L2",
    ),
    "stats equated to a calendar that is cut in the or": (
        scaffold(where="WHERE sc.midnight < sc.label_ts OR sc.user_id = 1"),
        "L2",
    ),
}


@pytest.mark.parametrize("sql, rule", SCAFFOLD_LEAKY.values(), ids=SCAFFOLD_LEAKY.keys())
def test_a_calendar_that_is_not_cut_stays_flagged(sql, rule):
    verdict, rules = check(sql)
    assert verdict == "leaky" and rule in rules, (verdict, rules)


# --- a merged timeline of spine rows and facts (L2, L5) -------------------------------------

TIMELINE = """WITH tl AS (
  SELECT user_id, label_ts AS ts, {spine_flag} AS flag, churned, CAST(NULL AS DOUBLE) AS v
  FROM labels
  UNION ALL
  SELECT user_id, {fact_ts} AS ts, {fact_flag} AS flag, CAST(NULL AS BIGINT) AS churned, {value}
  FROM {fact_table}),
run AS (
  SELECT user_id, ts, flag, churned,
         {fn} OVER (PARTITION BY user_id ORDER BY ts{tie} {frame}) AS carried
  FROM tl)
SELECT user_id, ts AS label_ts, churned, COALESCE(carried, 0) AS carried
FROM run {where}"""
UNTIL_NOW = "ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW"


def timeline(**overrides):
    parts = {
        "spine_flag": "0",
        "fact_flag": "1",
        "fact_ts": "ingested_at",
        "fact_table": "events",
        "value": "amount AS v",
        "fn": "SUM(v)",
        "tie": ", flag",
        "frame": UNTIL_NOW,
        "where": "WHERE flag = 0",
    }
    return TIMELINE.format(**{**parts, **overrides})


FORWARD_FILL = {
    "fact_ts": "updated_at",
    "fact_table": "user_stats",
    "value": "CAST(total_spend AS DOUBLE) AS v",
    "fn": "last_value(v IGNORE NULLS)",
}
BOOLEAN_FLAG = {
    "spine_flag": "TRUE",
    "fact_flag": "FALSE",
    "tie": ", flag DESC",
    "where": "WHERE flag",
}

TIMELINE_CLEAN = {
    "running sum, spine sorted first": timeline(),
    "running sum over the last rows only": timeline(
        frame="ROWS BETWEEN 9 PRECEDING AND CURRENT ROW"
    ),
    "default range frame, spine sorted first": timeline(frame=""),
    "forward fill of stats": timeline(**FORWARD_FILL),
    "boolean flag, spine first": timeline(**BOOLEAN_FLAG),
    "facts sorted first under an inclusive boundary": timeline(
        spine_flag="1", fact_flag="0", where="WHERE flag = 1"
    ),
}


@pytest.mark.parametrize("sql", TIMELINE_CLEAN.values(), ids=TIMELINE_CLEAN.keys())
def test_a_running_window_over_a_merged_timeline_bounds_the_facts(sql):
    inclusive = "facts sorted first" in sql or "flag = 1" in sql
    assert check(sql, boundary="inclusive" if inclusive else "strict") == ("clean", set())


TIMELINE_LEAKY = {
    "no tie-break: facts at the same instant may come first": (timeline(tie=""), "L5"),
    "tie-break sorts facts first": (
        timeline(spine_flag="1", fact_flag="0", where="WHERE flag = 1"),
        "L5",
    ),
    "tie-break by a column the branches compute": (timeline(tie=", churned"), "L5"),
    "range frame on a bare time key": (
        timeline(tie="", frame="RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW"),
        "L5",
    ),
    "frame reaches the next rows": (
        timeline(frame="ROWS BETWEEN UNBOUNDED PRECEDING AND 1 FOLLOWING"),
        "L2",
    ),
    "frame is the whole partition": (
        timeline(frame="ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING"),
        "L2",
    ),
    "facts placed at their event time": (timeline(fact_ts="event_ts"), "L4"),
    "forward fill that looks ahead": (
        timeline(
            **{
                **FORWARD_FILL,
                "fn": "first_value(v IGNORE NULLS)",
                "frame": "ROWS BETWEEN CURRENT ROW AND UNBOUNDED FOLLOWING",
            }
        ),
        "L2",
    ),
    "descending time": (
        timeline().replace("ORDER BY ts, flag", "ORDER BY ts DESC, flag"),
        "L2",
    ),
}


@pytest.mark.parametrize("sql, rule", TIMELINE_LEAKY.values(), ids=TIMELINE_LEAKY.keys())
def test_a_timeline_whose_order_admits_later_facts_stays_flagged(sql, rule):
    verdict, rules = check(sql)
    assert verdict != "clean" and rule in rules, (verdict, rules)


def test_a_timeline_read_without_picking_the_spine_rows_is_not_a_clean_spine():
    for where in ("", "WHERE flag = 1"):
        verdict, rules = check(timeline(where=where))
        assert verdict != "clean" and "SPEC" in rules, (where, verdict, rules)


def test_the_union_branch_a_filter_rules_out_is_not_read():
    """Only a WHERE on a constant the branch computes drops a branch; a filter on anything else
    (here a value, not a constant of the branch) leaves both in."""
    sql = timeline(where="WHERE carried > 0")
    assert "SPEC" in check(sql)[1]


# --- a named window (L1, L3) ------------------------------------------------------------

NAMED = """SELECT user_id, label_ts, churned, {expr} AS f
FROM labels WINDOW w AS (PARTITION BY user_id{order})"""
INLINE = """SELECT user_id, label_ts, churned, {expr} AS f FROM labels"""
BEHIND = "date_diff('hour', LAG(label_ts, 1) OVER {win}, label_ts)"


def named(expr, order=" ORDER BY label_ts"):
    return NAMED.format(expr=expr.format(win="w"), order=order)


def inline(expr, order=" ORDER BY label_ts"):
    return INLINE.format(
        expr=expr.format(win=f"(PARTITION BY user_id{order})"),
    )


@pytest.mark.parametrize("build", [named, inline], ids=["named window", "inline window"])
def test_a_window_over_earlier_prediction_times_is_clean(build):
    assert check(build(BEHIND)) == ("clean", set())
    assert check(build("row_number() OVER {win} - 1")) == ("clean", set())


@pytest.mark.parametrize("build", [named, inline], ids=["named window", "inline window"])
@pytest.mark.parametrize(
    "expr, order, rule",
    [
        ("date_diff('hour', label_ts, LEAD(label_ts, 1) OVER {win})", " ORDER BY label_ts", "L1"),
        ("LAG(label_ts, -1) OVER {win}", " ORDER BY label_ts", "L1"),
        (BEHIND, " ORDER BY label_ts DESC", "L1"),
        ("COUNT(*) OVER {win}", "", "L3"),
        (
            "COUNT(*) OVER {win}",
            " ORDER BY label_ts ROWS BETWEEN CURRENT ROW AND UNBOUNDED FOLLOWING",
            "L1",
        ),
    ],
    ids=["lead", "negative lag", "descending order", "whole partition", "frame to the end"],
)
def test_a_window_that_reads_later_prediction_times_stays_flagged(build, expr, order, rule):
    verdict, rules = check(build(expr, order))
    assert verdict == "leaky" and rule in rules, (verdict, rules)


# --- peers whose labels are final (L6) ----------------------------------------------------

PEERS = """WITH {chain}g AS (
  SELECT l.user_id, l.label_ts, l.churned, en.country FROM {source} l
  JOIN entities en ON en.user_id = l.user_id)
SELECT a.user_id, a.label_ts, a.churned, {extra}COUNT(b.churned) AS n, AVG(b.churned) AS rate
FROM g a LEFT JOIN g b ON b.country = a.country AND {bound}
GROUP BY a.user_id, a.label_ts, a.churned"""
KNOWN = {"label_known_after": "14 days"}


def peers(bound="b.label_ts + INTERVAL 14 DAY < a.label_ts", extra="", deep=False):
    chain = "base AS (SELECT user_id, label_ts, churned FROM labels), " if deep else ""
    return PEERS.format(chain=chain, source="base" if deep else "labels", extra=extra, bound=bound)


@pytest.mark.parametrize("deep", [False, True], ids=["one cte", "cte over cte"])
@pytest.mark.parametrize(
    "bound",
    [
        "b.label_ts + INTERVAL 14 DAY < a.label_ts",
        "b.label_ts + INTERVAL 21 DAY < a.label_ts",
        "b.label_ts < a.label_ts - INTERVAL 15 DAY",
        "a.label_ts > b.label_ts + INTERVAL 30 DAY",
    ],
)
def test_peers_whose_labels_are_final_are_clean_through_a_shared_cte(bound, deep):
    assert check(peers(bound, deep=deep), **KNOWN) == ("clean", set())


@pytest.mark.parametrize("deep", [False, True], ids=["one cte", "cte over cte"])
@pytest.mark.parametrize(
    "bound",
    [
        "b.label_ts + INTERVAL 13 DAY < a.label_ts",
        "b.label_ts + INTERVAL 1 DAY < a.label_ts",
        "b.label_ts + INTERVAL 14 DAY <= a.label_ts",  # final exactly at the prediction: not known
        "b.label_ts < a.label_ts",
        "b.label_ts + INTERVAL 14 DAY < a.label_ts + INTERVAL 1 DAY",
    ],
)
def test_peers_whose_labels_may_not_be_final_stay_leaky(bound, deep):
    verdict, rules = check(peers(bound, deep=deep), **KNOWN)
    assert verdict == "leaky" and "L6" in rules, (verdict, rules)


@pytest.mark.parametrize("deep", [False, True], ids=["one cte", "cte over cte"])
def test_the_matured_uses_get_copies_of_their_ctes_that_lineage_can_follow(deep):
    """The peers' feature traces to the renamed label table through the copied CTEs, the spine's
    own columns to the real one, and nothing traces to a name that is not a table."""
    analysis = analyze(peers(deep=deep), LeakageSpec.from_dict({**SPEC, **KNOWN}), SCHEMA)
    tree, schema = analysis.lineage_input()
    lineage = full_lineage(tree, schema, "duckdb")
    assert lineage["rate"] == {SourceColumn(MATURED_LABELS, "churned")}
    assert lineage["churned"] == {SourceColumn("labels", "churned")}
    assert {s.table for sources in lineage.values() for s in sources} <= set(schema)


def test_peer_labels_are_not_final_unless_the_spec_says_when():
    assert check(peers())[0] == "leaky"


def test_peer_maturity_follows_the_boundary_option():
    at_horizon = peers("b.label_ts + INTERVAL 14 DAY <= a.label_ts")
    assert check(at_horizon, **KNOWN, boundary="inclusive") == ("clean", set())


@pytest.mark.parametrize(
    "extra",
    ["a.churned AS own_label, ", "a.churned + AVG(b.churned) AS mixed, "],
)
def test_the_spine_label_stays_the_target_beside_final_peers(extra):
    verdict, rules = check(
        peers(extra=extra).replace("GROUP BY a.user_id", "GROUP BY a.churned, a.user_id"), **KNOWN
    )
    assert verdict == "leaky" and "L6" in rules, (verdict, rules)


def test_peer_labels_that_are_final_on_one_path_and_not_on_another_stay_leaky():
    final = "b.label_ts + INTERVAL 14 DAY < a.label_ts"
    sql = peers(final).replace(
        "GROUP BY",
        "LEFT JOIN g c ON c.country = a.country AND c.label_ts < a.label_ts GROUP BY",
    )
    sql = sql.replace("AVG(b.churned) AS rate", "AVG(c.churned) AS rate")
    verdict, rules = check(sql, **KNOWN)
    assert verdict == "leaky" and "L6" in rules


# --- the same query written through a staging cte or a split spine -------------------------

STAGED_FILTER = """WITH stg_events AS (SELECT * FROM events)
SELECT l.user_id, l.label_ts, l.churned,
       SUM(e.amount) FILTER (WHERE e.ingested_at < l.label_ts) AS s{extra}
FROM labels l LEFT JOIN stg_events e ON e.user_id = l.user_id
GROUP BY l.user_id, l.label_ts, l.churned"""
STAGED_LATEST = """WITH stg AS (SELECT user_id, updated_at, total_spend FROM user_stats)
SELECT l.user_id, l.label_ts, l.churned, s.total_spend
FROM labels l JOIN stg s ON s.user_id = l.user_id
WHERE s.updated_at = (SELECT MAX(x.updated_at) FROM stg x WHERE x.user_id = l.user_id{bound})"""
SPLIT_SPINE = """SELECT l.user_id, l.label_ts, l.churned, {feature} AS f
FROM (SELECT * FROM labels WHERE user_id % 2 = 0 UNION ALL
      SELECT * FROM labels WHERE user_id % 2 = 1) l{where}"""


def test_filter_bounds_and_latest_row_bounds_are_followed_through_a_staging_cte():
    assert check(STAGED_FILTER.format(extra="")) == ("clean", set())
    assert check(STAGED_LATEST.format(bound=" AND x.updated_at < l.label_ts")) == ("clean", set())
    # one aggregate that reads the table unfiltered, and a latest row with no bound
    unfiltered = STAGED_FILTER.format(extra=", COUNT(e.event_id) AS n")
    assert check(unfiltered)[0] == "leaky"
    assert check(STAGED_LATEST.format(bound=""))[0] == "leaky"


def test_a_split_spine_is_the_spine_for_windows_and_clock_filters():
    lag = "LAG(l.label_ts, 1) OVER (PARTITION BY l.user_id ORDER BY l.label_ts)"
    assert check(SPLIT_SPINE.format(feature=lag, where="")) == ("clean", set())
    lead = lag.replace("LAG", "LEAD")
    assert check(SPLIT_SPINE.format(feature=lead, where=""))[0] == "leaky"
    newest = "row_number() OVER (PARTITION BY l.user_id ORDER BY l.label_ts DESC)"
    assert "L1" in check(SPLIT_SPINE.format(feature=newest, where=""))[1]
    pick = " WHERE l.label_ts < now() + INTERVAL 100 YEAR"
    assert check(SPLIT_SPINE.format(feature="1", where=pick)) == ("clean", set())
    feature = SPLIT_SPINE.format(feature="CASE WHEN l.label_ts < now() THEN 1 ELSE 0 END", where="")
    assert check(feature)[1] == {"L7"}


DAY_KEY_SELF_JOIN = """WITH sp AS (SELECT user_id, label_ts, churned,
   md5(CAST(CAST(label_ts AS DATE) AS VARCHAR)) AS dk FROM labels)
SELECT a.user_id, a.label_ts, a.churned, MAX(b.label_ts) AS last_pred_today
FROM sp a JOIN sp b ON b.dk = a.dk GROUP BY 1, 2, 3"""


def test_a_self_join_on_a_key_of_the_day_reads_later_predictions():
    verdict, rules = check(DAY_KEY_SELF_JOIN)
    assert verdict == "leaky" and "L2" in rules
    keyed_on_row = DAY_KEY_SELF_JOIN.replace(
        "md5(CAST(CAST(label_ts AS DATE) AS VARCHAR))",
        "md5(CAST(user_id AS VARCHAR) || CAST(label_ts AS VARCHAR))",
    )
    assert check(keyed_on_row)[0] == "clean"  # the same row on both sides


# --- constants in a timeline's tie-break and in a branch filter are read literally ----------------


@pytest.mark.parametrize("spine_ord", ["CAST(0.6 AS INT)", "TRY_CAST(0.5 AS INT)", "0::INT"])
def test_a_cast_tie_break_is_not_a_constant_that_sorts_the_spine_first(spine_ord):
    """The engine rounds CAST(0.6 AS INT) to 1: the spine ties with the facts' 1 and may sort
    after them, so the bound is inclusive (L5), never the strict one a bare 0 earns."""
    verdict, rules = check(timeline(spine_flag=spine_ord))
    assert verdict == "leaky" and "L5" in rules, (verdict, rules)
    assert check(timeline(spine_flag="0")) == ("clean", set())


def test_a_cast_flag_does_not_prune_a_union_branch():
    sql = timeline(spine_flag="'0'", where="WHERE flag = CAST('0' AS INT)")
    assert check(sql)[0] != "clean"


# --- a timeline bound belongs to the window that earns it ----------------------------------

TIMELINE_EXTRA = {
    "second whole-partition window in the same select": timeline(
        tie=", flag", where="WHERE flag = 0"
    ).replace(
        "AS carried\n  FROM tl",
        "AS carried, SUM(v) OVER (PARTITION BY user_id) AS total_all\n  FROM tl",
    ),
    "window over the carried values": timeline().replace(
        "SELECT user_id, ts AS label_ts, churned, COALESCE(carried, 0) AS carried",
        "SELECT user_id, ts AS label_ts, churned, "
        "MAX(carried) OVER (PARTITION BY user_id) AS final_carried",
    ),
    "aggregate over the carried values": timeline().replace(
        "SELECT user_id, ts AS label_ts, churned, COALESCE(carried, 0) AS carried",
        "SELECT user_id, ts AS label_ts, churned, MAX(carried) AS final_carried",
    )
    + " GROUP BY user_id, ts, churned",
}


@pytest.mark.parametrize("sql", TIMELINE_EXTRA.values(), ids=TIMELINE_EXTRA.keys())
def test_other_windows_and_aggregates_over_a_timeline_get_no_exemption(sql):
    assert check(timeline())[0] == "clean"
    verdict, rules = check(sql)
    assert verdict == "leaky" and rules & {"L2", "L3", "L1"}, (verdict, rules)


def test_only_lossless_casts_may_make_a_key():
    from sqlglot import parse_one

    from sqlguard.leakage.analysis import _lossless_key_cast

    def cast(sql: str, dialect: str = "postgres") -> bool:
        return _lossless_key_cast(parse_one(f"SELECT {sql}", read=dialect).selects[0])

    assert cast("CAST(label_ts AS VARCHAR)") and cast("label_ts::text")
    assert cast("CAST(label_ts AS TIMESTAMP)") and cast("CAST(label_ts AS TIMESTAMPTZ)")
    assert not cast("CAST(label_ts AS VARCHAR(10))")  # truncates where the dialect enforces it
    assert not cast("CAST(label_ts AS DATE)") and not cast("CAST(user_id AS INT)")
    assert not cast("TRY_CAST(label_ts AS DATE)")
