"""Which output columns a node of the query feeds, and the findings that follow from it.

A finding that provably feeds only label or metadata columns, and decides no row of the output,
is shown as INFO with weight 0. Everything else keeps its severity: every demotion here is paired
with a query of the same shape that also feeds a feature, filters, joins or groups rows, or whose
trace cannot be completed, and that must stay flagged."""

import gc

import pytest
from sqlglot import exp

from sqlguard.core.findings import Severity
from sqlguard.leakage import roles
from sqlguard.leakage.analysis import analyze
from sqlguard.leakage.roles import Attribution, attribute, feeds, non_feature_only
from sqlguard.leakage.spec import LeakageSpec
from sqlguard.leakage.validator import Weights, analysis_findings, validate

from .conftest import SCHEMA, SPEC

KEYS = "l.user_id, l.label_ts, l.churned"


def spec_with(roles: dict | None = None, **overrides) -> LeakageSpec:
    extra = {"output_roles": roles} if roles else {}
    return LeakageSpec.from_dict({**SPEC, **extra, **overrides})


def clock(sql: str, **kwargs):
    analysis = analyze(sql, spec_with(**kwargs), SCHEMA)
    node = analysis.qualified.find(exp.CurrentDate, exp.CurrentTimestamp)
    assert node is not None
    return analysis, node


def reading(sql: str) -> tuple[list[str], bool]:
    analysis, node = clock(sql)
    got = feeds(analysis, node)
    return sorted(got.outputs), got.row_level


# --- which outputs a node feeds ---------------------------------------------------------------

FEEDS = {
    "a projection of the output": (
        f"SELECT {KEYS}, CURRENT_DATE AS stamp FROM labels l",
        (["stamp"], False),
    ),
    "inside an expression": (
        f"SELECT {KEYS}, DATEDIFF('day', l.label_ts, CURRENT_DATE) + 1 AS age FROM labels l",
        (["age"], False),
    ),
    "the name is the alias, lowercased": (
        f"SELECT {KEYS}, CURRENT_DATE AS Stamp FROM labels l",
        (["stamp"], False),
    ),
    "renamed through a cte": (
        f"""WITH c AS (SELECT {KEYS}, CURRENT_DATE AS d FROM labels l)
SELECT user_id, label_ts, churned, d AS stamp FROM c""",
        (["stamp"], False),
    ),
    "through a derived table": (
        f"""SELECT q.user_id, q.label_ts, q.churned, q.d AS stamp
FROM (SELECT {KEYS}, CURRENT_DATE AS d FROM labels l) q""",
        (["stamp"], False),
    ),
    "through two ctes, inside an expression": (
        f"""WITH a AS (SELECT {KEYS}, CURRENT_DATE AS d FROM labels l),
b AS (SELECT user_id, label_ts, churned, d AS e FROM a)
SELECT user_id, label_ts, churned, e || 'x' AS stamp FROM b""",
        (["stamp"], False),
    ),
    "a cte column used by two outputs": (
        f"""WITH c AS (SELECT {KEYS}, CURRENT_DATE AS d FROM labels l)
SELECT user_id, label_ts, churned, d AS one, d + 1 AS two FROM c""",
        (["one", "two"], False),
    ),
    "a cte column the output never selects": (
        f"""WITH c AS (SELECT {KEYS}, CURRENT_DATE AS d FROM labels l)
SELECT user_id, label_ts, churned FROM c""",
        ([], False),
    ),
    "a star expands to the columns": (
        f"""WITH c AS (SELECT {KEYS}, CURRENT_DATE AS d FROM labels l)
SELECT * FROM c""",
        (["d"], False),
    ),
    "a scalar subquery in a projection": (
        f"""SELECT {KEYS},
  (SELECT MAX(e.event_ts) FROM events e WHERE e.event_ts < CURRENT_DATE) AS last_event
FROM labels l""",
        (["last_event"], False),
    ),
    "an exists in a projection": (
        f"""SELECT {KEYS},
  EXISTS (SELECT 1 FROM events e WHERE e.user_id = l.user_id
    AND e.event_ts < CURRENT_DATE) AS seen
FROM labels l""",
        (["seen"], False),
    ),
    "an order by without a limit": (
        f"SELECT {KEYS}, CURRENT_DATE AS stamp FROM labels l ORDER BY stamp",
        (["stamp"], False),
    ),
}

ROW_LEVEL = {
    "where of the output": (
        f"SELECT {KEYS} FROM labels l WHERE l.label_ts < CURRENT_DATE",
        ([], True),
    ),
    "join condition": (
        f"""SELECT {KEYS}, e.amount FROM labels l
JOIN events e ON e.user_id = l.user_id AND e.event_ts < CURRENT_DATE""",
        ([], True),
    ),
    "having": (
        f"""SELECT {KEYS}, COUNT(*) AS n FROM labels l GROUP BY l.user_id, l.label_ts, l.churned
HAVING MAX(l.label_ts) < CURRENT_DATE""",
        ([], True),
    ),
    "qualify": (
        f"""SELECT {KEYS} FROM labels l
QUALIFY l.label_ts < CURRENT_DATE""",
        ([], True),
    ),
    "group by": (
        """SELECT l.user_id, l.label_ts, COUNT(*) AS n FROM labels l
GROUP BY l.user_id, l.label_ts, CURRENT_DATE""",
        ([], True),
    ),
    "partition by of a window": (
        f"""SELECT {KEYS}, ROW_NUMBER() OVER (PARTITION BY CURRENT_DATE ORDER BY l.label_ts) AS rn
FROM labels l""",
        (["rn"], True),
    ),
    "order by of a window": (
        f"""SELECT {KEYS}, ROW_NUMBER() OVER (ORDER BY CURRENT_DATE) AS rn FROM labels l""",
        (["rn"], True),
    ),
    "select distinct": (
        f"SELECT DISTINCT {KEYS}, CURRENT_DATE AS stamp FROM labels l",
        (["stamp"], True),
    ),
    "order by with a limit": (
        f"SELECT {KEYS}, CURRENT_DATE AS stamp FROM labels l ORDER BY stamp LIMIT 10",
        (["stamp"], True),
    ),
    "a cte column that the consumer filters on": (
        f"""WITH c AS (SELECT {KEYS}, CURRENT_DATE AS d FROM labels l)
SELECT user_id, label_ts, churned FROM c WHERE d > DATE '2020-01-01'""",
        ([], True),
    ),
    "a cte column that the consumer joins on": (
        f"""WITH c AS (SELECT {KEYS}, CURRENT_DATE AS d FROM labels l)
SELECT c.user_id, c.label_ts, c.churned, e.amount FROM c
JOIN events e ON e.user_id = c.user_id AND e.event_ts < c.d""",
        ([], True),
    ),
    "a cte column that a later cte filters on, with an output besides": (
        f"""WITH c AS (SELECT {KEYS}, CURRENT_DATE AS d FROM labels l),
f AS (SELECT user_id, label_ts, churned, d AS keep FROM c WHERE d > DATE '2020-01-01')
SELECT user_id, label_ts, churned, keep FROM f""",
        (["keep"], True),
    ),
    "a rank column used to pick one row": (
        """WITH r AS (SELECT s.user_id, s.updated_at, s.total_spend,
    ROW_NUMBER() OVER (PARTITION BY s.user_id ORDER BY s.updated_at DESC, CURRENT_DATE) AS rn
  FROM user_stats s)
SELECT l.user_id, l.label_ts, l.churned, r.total_spend
FROM labels l JOIN r ON r.user_id = l.user_id AND r.rn = 1""",
        ([], True),
    ),
    "in a subquery of a where": (
        f"""SELECT {KEYS} FROM labels l WHERE l.user_id IN
  (SELECT e.user_id FROM events e WHERE e.event_ts < CURRENT_DATE)""",
        ([], True),
    ),
    "in a subquery of a join": (
        f"""SELECT {KEYS}, q.amount FROM labels l JOIN
  (SELECT e.user_id, e.amount FROM events e WHERE e.event_ts < CURRENT_DATE) q
  ON q.user_id = l.user_id""",
        ([], True),
    ),
    "group by all": (
        f"SELECT {KEYS}, CURRENT_DATE AS stamp FROM labels l GROUP BY ALL",
        (["stamp"], True),
    ),
    "two outputs with one name": (
        f"SELECT {KEYS}, CURRENT_DATE AS dup, 1 AS dup FROM labels l",
        (["dup"], True),
    ),
    "a cte column that a pivot reads": (
        f"""WITH c AS (SELECT {KEYS}, CURRENT_DATE AS d FROM labels l)
SELECT * FROM c PIVOT (MAX(d) FOR churned IN (0, 1))""",
        None,
    ),
    "a whole-row reference to the cte": (
        f"""WITH c AS (SELECT {KEYS}, CURRENT_DATE AS nxt FROM labels l)
SELECT c.user_id, c.label_ts, c.churned, c.nxt, to_json(c) AS feat FROM c""",
        None,
    ),
    "the cte as a struct column": (
        f"""WITH c AS (SELECT {KEYS}, CURRENT_DATE AS nxt FROM labels l)
SELECT c.user_id, c.label_ts, c.churned, c.nxt, c AS feat FROM c""",
        None,
    ),
    "a column pattern over the cte": (
        f"""WITH c AS (SELECT {KEYS}, CURRENT_DATE AS nxt FROM labels l)
SELECT c.user_id, c.label_ts, c.churned, COLUMNS('nx.*') + 1 FROM c""",
        None,
    ),
    "a bounded order by on a union": (
        f"""SELECT {KEYS}, CURRENT_DATE AS nxt FROM labels l
UNION ALL SELECT {KEYS}, CURRENT_DATE FROM labels l ORDER BY nxt DESC LIMIT 100""",
        (["nxt"], True),
    ),
    "a set-returning function over the value": (
        f"""WITH c AS (
  SELECT {KEYS}, DATEDIFF('day', DATE '2020-01-01', CURRENT_DATE) AS n FROM labels l)
SELECT c.user_id, c.label_ts, c.churned, UNNEST(range(CAST(c.n AS BIGINT))) AS spread FROM c""",
        (["spread"], True),
    ),
    "an order by in a cte that a later limit cuts": (
        f"""WITH c AS (SELECT {KEYS}, CURRENT_DATE AS nxt FROM labels l ORDER BY nxt DESC)
SELECT c.user_id, c.label_ts, c.churned, c.nxt FROM c LIMIT 10""",
        (["nxt"], True),
    ),
    "an order by in a derived table that an outer limit cuts": (
        f"""SELECT * FROM (SELECT {KEYS}, CURRENT_DATE AS nxt FROM labels l ORDER BY nxt DESC) q
LIMIT 10""",
        (["nxt"], True),
    ),
    "a recursive cte": (
        f"""WITH RECURSIVE r AS (
  SELECT {KEYS}, CURRENT_DATE AS nxt, 0 AS n FROM labels l
  UNION ALL SELECT r.user_id, r.label_ts, r.churned, r.nxt, r.n + 1 FROM r
  WHERE r.nxt > DATE '2020-01-01' AND r.n < 3)
SELECT r.user_id, r.label_ts, r.churned, r.nxt FROM r""",
        None,
    ),
    "a union that removes duplicates": (
        f"""SELECT {KEYS}, CURRENT_DATE AS stamp FROM labels l
UNION SELECT {KEYS}, CAST(NULL AS DATE) FROM labels l""",
        (["stamp"], True),
    ),
    "an except": (
        f"""SELECT {KEYS}, CURRENT_DATE AS stamp FROM labels l
EXCEPT SELECT {KEYS}, CAST(NULL AS DATE) FROM labels l""",
        (["stamp"], True),
    ),
}


@pytest.mark.parametrize("sql, expected", FEEDS.values(), ids=FEEDS.keys())
def test_outputs_a_node_feeds(sql, expected):
    assert reading(sql) == expected


@pytest.mark.parametrize("sql, expected", ROW_LEVEL.values(), ids=ROW_LEVEL.keys())
def test_a_node_that_decides_rows_is_row_level(sql, expected):
    got = reading(sql)
    # a shape the qualifier may rewrite beyond recognition is only required to be row level
    assert got == expected if expected is not None else got[1] is True


UNION_ALL = """SELECT {keys}, 1.0 AS score, 2.0 AS other FROM labels l
UNION ALL
SELECT {keys}, {second}, {third} FROM labels l"""


def test_union_branches_line_up_by_position_not_by_name():
    # the second branch calls its fourth column `other` and its fifth `score`; the output names
    # come from the first branch, so a node in the second branch's fourth column feeds `score`
    sql = UNION_ALL.format(keys=KEYS, second="CURRENT_DATE AS other", third="2.0 AS score")
    assert reading(sql) == (["score"], False)
    sql = UNION_ALL.format(keys=KEYS, second="2.0 AS other", third="CURRENT_DATE AS score")
    assert reading(sql) == (["other"], False)


def test_a_union_inside_a_cte_is_followed_to_the_output():
    sql = f"""WITH u AS (
  SELECT {KEYS}, 1.0 AS score FROM labels l
  UNION ALL
  SELECT {KEYS}, CURRENT_DATE FROM labels l)
SELECT user_id, label_ts, churned, score AS final_score FROM u"""
    assert reading(sql) == (["final_score"], False)


def test_a_union_all_nested_in_another_is_followed():
    sql = f"""SELECT {KEYS}, 1.0 AS score FROM labels l
UNION ALL (SELECT {KEYS}, 2.0 FROM labels l UNION ALL SELECT {KEYS}, CURRENT_DATE FROM labels l)"""
    assert reading(sql) == (["score"], False)


def test_a_cte_read_by_both_branches_of_a_union_feeds_both_outputs():
    sql = f"""WITH c AS (SELECT {KEYS}, CURRENT_DATE AS d FROM labels l)
SELECT user_id, label_ts, churned, d AS one FROM c
UNION ALL SELECT user_id, label_ts, churned, CAST(d AS DATE) FROM c"""
    assert reading(sql) == (["one"], False)


def test_the_same_cte_column_under_two_aliases_is_traced_under_each():
    sql = f"""WITH c AS (SELECT {KEYS}, CURRENT_DATE AS d FROM labels l)
SELECT a.user_id, a.label_ts, a.churned, a.d AS first, b.d AS second
FROM c a JOIN c b ON a.user_id = b.user_id AND a.label_ts = b.label_ts"""
    assert reading(sql) == (["first", "second"], False)


def test_an_inner_alias_that_shadows_the_cte_alias_is_not_the_cte():
    # `c` inside the subquery is the events table, so its `d`-named column is not the cte's
    sql = f"""WITH c AS (SELECT {KEYS}, CURRENT_DATE AS amount FROM labels l)
SELECT c.user_id, c.label_ts, c.churned, {{shown}} FROM c"""
    shadowed = sql.format(shown="(SELECT MAX(c.amount) FROM events c) AS biggest")
    assert reading(shadowed) == ([], False)
    assert reading(sql.format(shown="c.amount AS shown")) == (["shown"], False)


def test_a_node_in_an_unused_cte_cannot_be_attributed():
    sql = f"""WITH unused AS (SELECT CURRENT_DATE AS d FROM labels l)
SELECT {KEYS} FROM labels l"""
    analysis, node = clock(sql)
    assert feeds(analysis, node) == Attribution(frozenset(), True)


# --- demotion: the clock rule ------------------------------------------------------------------

ROLES = {"metadata": ["dbt_updated_at"]}
METADATA = f"SELECT {KEYS}, CURRENT_TIMESTAMP AS dbt_updated_at FROM labels l"
RECENCY = f"""SELECT {KEYS}, DATEDIFF('day', l.label_ts, CURRENT_DATE) AS recency FROM labels l"""


def findings(sql: str, roles: dict | None = None, **overrides):
    spec = spec_with(roles, **overrides)
    return analysis_findings(analyze(sql, spec, SCHEMA))


def of_rule(found, rule: str):
    return [f for f in found if f.rule_id == rule]


def test_a_clock_in_a_declared_metadata_column_is_info_and_costs_nothing():
    assert [f.severity for f in of_rule(findings(METADATA), "L7")] == [Severity.HIGH]
    (finding,) = of_rule(findings(METADATA, ROLES), "L7")
    assert finding.severity is Severity.INFO
    assert finding.evidence["non_feature_only"] is True
    assert finding.evidence["outputs"] == ["dbt_updated_at"]
    assert "dbt_updated_at" in finding.message
    assert Weights.load().weight(finding) == 0.0
    report = validate(METADATA, spec_with(ROLES), schema=SCHEMA)
    assert report.verdict == "clean" and report.score == 0.0 and report.leak_types == []


def test_a_clock_in_a_feature_stays_flagged_whatever_else_is_declared():
    (finding,) = of_rule(findings(RECENCY, ROLES), "L7")
    assert finding.severity is Severity.HIGH
    assert "non_feature_only" not in finding.evidence
    assert finding.evidence["outputs"] == ["recency"]
    report = validate(RECENCY, spec_with(ROLES), schema=SCHEMA)
    assert report.verdict == "leaky" and report.leak_types == ["L7"]


def test_a_clock_that_feeds_a_declared_and_an_undeclared_column_stays_flagged():
    # two separate clock calls, one per output; each is judged by the column it feeds
    mixed = f"SELECT {KEYS}, CURRENT_DATE AS dbt_updated_at, CURRENT_DATE AS recency FROM labels l"
    states = sorted(f.severity.value for f in of_rule(findings(mixed, ROLES), "L7"))
    assert states == ["high", "info"]


def test_one_clock_value_reused_by_a_feature_and_by_metadata_stays_flagged():
    sql = f"""WITH c AS (SELECT {KEYS}, CURRENT_TIMESTAMP AS now_ts FROM labels l)
SELECT user_id, label_ts, churned, now_ts AS dbt_updated_at,
  DATEDIFF('day', label_ts, now_ts) AS age FROM c"""
    (finding,) = of_rule(findings(sql, ROLES), "L7")
    assert finding.severity is Severity.HIGH
    assert finding.evidence["outputs"] == ["age"]


def test_a_clock_in_a_staging_column_that_no_output_reads_is_dead_once_roles_are_declared():
    # the trace finishes without reaching an output or deciding a row: no feature can read it
    sql = f"""WITH stg AS (SELECT {KEYS}, CURRENT_TIMESTAMP AS loaded_at FROM labels l)
SELECT user_id, label_ts, churned FROM stg"""
    (finding,) = of_rule(findings(sql, ROLES), "L7")
    assert finding.severity is Severity.INFO and finding.evidence["outputs"] == []
    (kept,) = of_rule(findings(sql), "L7")  # a spec with no roles keeps it
    assert kept.severity is Severity.HIGH


def test_a_clock_in_metadata_that_also_filters_rows_stays_flagged():
    sql = f"""WITH stg AS (SELECT {KEYS}, CURRENT_TIMESTAMP AS dbt_updated_at FROM labels l)
SELECT user_id, label_ts, churned, dbt_updated_at FROM stg
WHERE dbt_updated_at > DATE '2020-01-01'"""
    (finding,) = of_rule(findings(sql, ROLES), "L7")
    assert finding.severity is Severity.HIGH


def test_a_clock_in_metadata_through_a_union_is_demoted_only_when_every_branch_is():
    sql = f"""SELECT {KEYS}, CURRENT_TIMESTAMP AS dbt_updated_at FROM labels l
UNION ALL SELECT {KEYS}, CAST(NULL AS TIMESTAMP) FROM labels l"""
    (finding,) = of_rule(findings(sql, ROLES), "L7")
    assert finding.severity is Severity.INFO
    distinct = sql.replace("UNION ALL", "UNION")
    (finding,) = of_rule(findings(distinct, ROLES), "L7")
    assert finding.severity is Severity.HIGH


def test_a_clock_in_metadata_through_a_subquery_in_a_filter_stays_flagged():
    sql = f"""SELECT {KEYS}, CURRENT_TIMESTAMP AS dbt_updated_at FROM labels l
WHERE l.user_id IN (SELECT e.user_id FROM events e WHERE e.event_ts < CURRENT_DATE)"""
    found = of_rule(findings(sql, ROLES), "L7")
    assert sorted(f.severity.value for f in found) == ["high", "info"]


# --- the allow-list of features -----------------------------------------------------------------


def test_with_a_feature_list_only_the_listed_columns_are_features():
    sql = f"""SELECT {KEYS}, CURRENT_DATE AS stamp, DATEDIFF('day', l.label_ts, CURRENT_DATE) AS age
FROM labels l"""
    found = of_rule(findings(sql, {"features": ["age"]}), "L7")
    assert sorted(f.severity.value for f in found) == ["high", "info"]
    (flagged,) = [f for f in found if f.severity is Severity.HIGH]
    assert flagged.evidence["outputs"] == ["age"]


def test_a_feature_list_does_not_demote_the_entity_key_or_the_prediction_time():
    sql = "SELECT CAST(CURRENT_DATE AS BIGINT) AS user_id, l.label_ts, l.churned FROM labels l"
    (finding,) = of_rule(findings(sql, {"features": ["age"]}), "L7")
    assert finding.severity is Severity.HIGH
    sql = "SELECT l.user_id, CAST(CURRENT_DATE AS TIMESTAMP) AS label_ts, l.churned FROM labels l"
    (finding,) = of_rule(findings(sql, {"features": ["age"]}), "L7")
    assert finding.severity is Severity.HIGH


# --- demotion: windows ---------------------------------------------------------------------------

LEAD = """WITH e AS (
  SELECT x.user_id, x.updated_at, x.total_spend,
    LEAD(x.total_spend) OVER (PARTITION BY x.user_id ORDER BY x.updated_at) AS next_amount
  FROM user_stats x)
SELECT l.user_id, l.label_ts, l.churned, e.next_amount{extra}
FROM labels l JOIN e ON e.user_id = l.user_id AND e.updated_at < l.label_ts"""


def test_a_lookahead_window_that_only_makes_the_label_is_info():
    sql = LEAD.format(extra="")
    assert [f.severity for f in of_rule(findings(sql), "L1")] == [Severity.HIGH]
    (finding,) = of_rule(findings(sql, {"label": ["next_amount"]}), "L1")
    assert finding.severity is Severity.INFO
    assert finding.evidence["outputs"] == ["next_amount"]
    report = validate(sql, spec_with({"label": ["next_amount"]}), schema=SCHEMA)
    assert report.verdict == "clean" and report.leak_types == []


def test_the_same_window_that_also_makes_a_feature_stays_flagged():
    sql = LEAD.format(extra=", e.next_amount * 2 AS feature")
    (finding,) = of_rule(findings(sql, {"label": ["next_amount"]}), "L1")
    assert finding.severity is Severity.HIGH
    assert finding.evidence["outputs"] == ["feature"]
    report = validate(sql, spec_with({"label": ["next_amount"]}), schema=SCHEMA)
    assert report.verdict == "leaky"


def test_a_lookahead_window_that_picks_rows_stays_flagged_even_for_a_metadata_column():
    sql = """WITH r AS (SELECT s.user_id, s.updated_at, s.total_spend,
    ROW_NUMBER() OVER (PARTITION BY s.user_id ORDER BY s.updated_at DESC) AS rn
  FROM user_stats s)
SELECT l.user_id, l.label_ts, l.churned, r.total_spend, r.rn AS pick
FROM labels l JOIN r ON r.user_id = l.user_id AND r.rn = 1"""
    found = of_rule(findings(sql, {"metadata": ["pick"]}), "L1")
    assert found and all(f.severity is Severity.HIGH for f in found)


def test_a_window_aggregate_over_the_whole_partition_follows_the_same_rule():
    sql = """WITH e AS (SELECT x.user_id, x.updated_at, x.total_spend,
    SUM(x.total_spend) OVER (PARTITION BY x.user_id) AS total FROM user_stats x)
SELECT l.user_id, l.label_ts, l.churned, e.total{extra}
FROM labels l JOIN e ON e.user_id = l.user_id AND e.updated_at < l.label_ts"""
    plain = sql.format(extra="")
    assert [f.severity for f in of_rule(findings(plain), "L3")] == [Severity.HIGH]
    (finding,) = of_rule(findings(plain, {"metadata": ["total"]}), "L3")
    assert finding.severity is Severity.INFO
    both = sql.format(extra=", e.total AS copy")
    (finding,) = of_rule(findings(both, {"metadata": ["total"]}), "L3")
    assert finding.severity is Severity.HIGH


# --- demotion: label use and label lineage -----------------------------------------------------

STATE = """WITH t AS (
  SELECT l.user_id, l.label_ts, l.churned,
    CASE WHEN l.churned = 1 THEN 'gone' ELSE 'stay' END AS state_transition
  FROM labels l)
SELECT t.user_id, t.label_ts, t.churned, t.state_transition{extra} FROM t{where}"""


def test_a_case_on_the_label_that_only_makes_a_label_column_is_not_a_leak():
    sql = STATE.format(extra="", where="")
    base = findings(sql)
    assert [f.severity for f in of_rule(base, "L6")] == [Severity.HIGH, Severity.HIGH]
    roles = {"label": ["state_transition"]}
    found = of_rule(findings(sql, roles), "L6")
    # the lineage of a declared label column is skipped; the use of the label is info
    assert [(f.severity, f.evidence.get("non_feature_only")) for f in found] == [
        (Severity.INFO, True)
    ]
    report = validate(sql, spec_with(roles), schema=SCHEMA)
    assert report.verdict == "clean" and report.score == 0.0


def test_a_case_on_the_label_that_selects_feature_rows_stays_flagged():
    # the label and a feature in one predicate: the label chooses which feature rows exist
    sql = """WITH t AS (
  SELECT l.user_id, l.label_ts, l.churned, e.amount,
    CASE WHEN l.churned = 1 THEN 'gone' ELSE 'stay' END AS state_transition
  FROM labels l JOIN events e ON e.user_id = l.user_id AND e.event_ts < l.label_ts
  WHERE CASE WHEN l.churned = 1 THEN e.amount ELSE 0 END > 0)
SELECT t.user_id, t.label_ts, t.churned, t.amount, t.state_transition FROM t"""
    roles = {"label": ["state_transition"]}
    found = of_rule(findings(sql, roles), "L6")
    assert any(f.severity is Severity.HIGH and "non_feature_only" not in f.evidence for f in found)
    assert validate(sql, spec_with(roles), schema=SCHEMA).verdict == "leaky"


def test_a_case_on_the_label_that_also_feeds_a_feature_stays_flagged():
    sql = STATE.format(extra=", length(t.state_transition) AS feature", where="")
    roles = {"label": ["state_transition"]}
    found = of_rule(findings(sql, roles), "L6")
    assert any(f.severity is Severity.HIGH for f in found)
    assert validate(sql, spec_with(roles), schema=SCHEMA).verdict == "leaky"


def test_an_alias_of_the_label_is_skipped_when_declared_and_flagged_when_it_is_a_feature():
    sql = f"SELECT {KEYS}, l.churned AS target_churned FROM labels l"
    assert [f.severity for f in of_rule(findings(sql), "L6")] == [Severity.HIGH]
    assert of_rule(findings(sql, {"label": ["target_churned"]}), "L6") == []
    doubled = f"SELECT {KEYS}, l.churned AS target_churned, l.churned * 2 AS feat FROM labels l"
    found = of_rule(findings(doubled, {"label": ["target_churned"]}), "L6")
    lineage = [f for f in found if "sources" in f.evidence]
    assert [f.location for f in lineage] == ["feat"]
    assert all(f.severity is Severity.HIGH for f in found)


def test_the_lineage_of_a_feature_outside_the_feature_list_is_skipped_and_one_inside_is_not():
    sql = f"SELECT {KEYS}, l.churned * 2 AS inside, l.churned * 3 AS outside FROM labels l"
    found = of_rule(findings(sql, {"features": ["inside"]}), "L6")
    assert [f.location for f in found if "sources" in f.evidence] == ["inside"]


# --- the validator ------------------------------------------------------------------------------


def test_a_demoted_finding_has_weight_zero_and_other_findings_keep_theirs():
    weights = Weights.load()
    (finding,) = of_rule(findings(METADATA, ROLES), "L7")
    assert weights.weight(finding) == 0.0
    (plain,) = of_rule(findings(METADATA), "L7")
    assert weights.weight(plain) == weights.rules["L7"] > 0


def test_a_demoted_finding_is_not_a_leak_type_but_stays_in_the_report():
    report = validate(METADATA, spec_with(ROLES), schema=SCHEMA)
    assert report.leak_types == []
    assert [f.rule_id for f in report.findings] == ["L7"]
    assert report.to_dict()["findings"][0]["severity"] == "info"


def test_a_demoted_finding_does_not_hide_a_real_one_in_the_same_query():
    sql = f"""SELECT {KEYS}, CURRENT_TIMESTAMP AS dbt_updated_at, e.amount
FROM labels l JOIN events e ON e.user_id = l.user_id"""
    report = validate(sql, spec_with(ROLES), schema=SCHEMA)
    assert report.verdict == "leaky"
    assert "L2" in report.leak_types and "L7" not in report.leak_types


# --- the helpers --------------------------------------------------------------------------------


def test_a_finding_without_a_node_is_returned_unchanged():
    analysis, _ = clock(METADATA, output_roles=ROLES)
    from sqlguard.core.findings import Finding

    finding = Finding("L7", Severity.HIGH, "message", "loc")
    assert attribute(analysis, finding, None) is finding


def test_non_feature_only_needs_outputs_and_no_row_level_use():
    spec_kwargs = {"output_roles": ROLES}
    analysis, node = clock(METADATA, **spec_kwargs)
    assert non_feature_only(analysis, node) is True
    analysis, node = clock(RECENCY, **spec_kwargs)
    assert non_feature_only(analysis, node) is False
    analysis, node = clock(f"SELECT {KEYS} FROM labels l WHERE l.label_ts < CURRENT_DATE")
    assert non_feature_only(analysis, node) is False


def test_without_declared_roles_nothing_is_ever_demoted():
    for sql in (METADATA, RECENCY, LEAD.format(extra=""), STATE.format(extra="", where="")):
        for finding in findings(sql):
            assert finding.severity is not Severity.INFO
            assert "non_feature_only" not in finding.evidence


def test_the_index_of_an_analysis_is_dropped_with_it():
    before = len(roles._FLOWS)
    analysis, node = clock(METADATA)
    feeds(analysis, node)
    assert len(roles._FLOWS) == before + 1
    del analysis, node
    gc.collect()
    assert len(roles._FLOWS) == before


# --- ways of reading a label column that the trace must not lose ---------------------------------

LEAD_CTE = """WITH c AS (
  SELECT l.user_id, l.label_ts, l.churned,
    LEAD(s.total_spend) OVER (PARTITION BY s.user_id ORDER BY s.updated_at) AS nxt
  FROM labels l JOIN user_stats s ON s.user_id = l.user_id AND s.updated_at < l.label_ts)
"""
READ_ELSEWHERE = {
    "the whole row of the cte as a feature": "SELECT c.user_id, c.label_ts, c.churned, c.nxt, "
    "to_json(c) AS feat FROM c",
    "columns picked by pattern": "SELECT c.user_id, c.label_ts, c.churned, c.nxt, "
    "COLUMNS('nx.*') + 1 FROM c",
    "a bounded order of a union": "SELECT c.user_id, c.label_ts, c.churned, c.nxt FROM c "
    "UNION ALL SELECT l.user_id, l.label_ts, l.churned, 0 FROM labels l "
    "ORDER BY nxt DESC LIMIT 100",
    "a row count that follows the value": "SELECT c.user_id, c.label_ts, c.churned, "
    "UNNEST(range(CAST(c.nxt AS BIGINT))) AS nxt FROM c",
    "an order in a derived table with the limit outside": "SELECT * FROM (SELECT c.user_id, "
    "c.label_ts, c.churned, c.nxt FROM c ORDER BY c.nxt DESC) q LIMIT 10",
}


@pytest.mark.parametrize("tail", READ_ELSEWHERE.values(), ids=READ_ELSEWHERE.keys())
def test_a_lookahead_window_read_in_a_way_the_trace_cannot_follow_stays_flagged(tail):
    sql = LEAD_CTE + tail
    plain = of_rule(findings(sql), "L1")
    declared = of_rule(findings(sql, {"label": ["nxt"]}), "L1")
    assert plain and declared
    assert all(f.severity is Severity.HIGH for f in declared)
    assert validate(sql, spec_with({"label": ["nxt"]}), schema=SCHEMA).verdict == "leaky"


def test_a_recursive_cte_is_never_traced():
    sql = """WITH RECURSIVE r AS (
  SELECT l.user_id, l.label_ts, l.churned, CURRENT_TIMESTAMP AS dbt_updated_at, 0 AS n FROM labels l
  UNION ALL SELECT r.user_id, r.label_ts, r.churned, r.dbt_updated_at, r.n + 1 FROM r
  WHERE r.dbt_updated_at > DATE '2020-01-01' AND r.n < 3)
SELECT r.user_id, r.label_ts, r.churned, r.dbt_updated_at FROM r"""
    (finding,) = of_rule(findings(sql, ROLES), "L7")
    assert finding.severity is Severity.HIGH
