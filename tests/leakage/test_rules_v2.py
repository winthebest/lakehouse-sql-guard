"""Shapes the first rule set misjudged (see docs/limitations.md), the spec fields that settle them,
and the wall-clock rule. Every exemption is paired with a query of the same shape and one wrong
condition, which must stay leaky."""

import pytest

from sqlguard.leakage.spec import LeakageSpec, SpecError
from sqlguard.leakage.validator import validate

from .conftest import SCHEMA, SPEC

SEL = "SELECT l.user_id, l.label_ts, l.churned"
LEFT = "FROM labels l LEFT JOIN events e ON e.user_id = l.user_id"
INNER = "FROM labels l JOIN events e ON e.user_id = l.user_id"
STATS = "ASOF JOIN user_stats s ON s.user_id = l.user_id AND l.label_ts > s.updated_at"
FEAT = f"{SEL}, s.total_spend FROM labels l {STATS}"
GROUP = "GROUP BY 1, 2, 3"
BEFORE = "FILTER (WHERE e.ingested_at < l.label_ts)"


def check(sql: str, **spec_overrides) -> tuple[str, set[str]]:
    spec = LeakageSpec.from_dict({**SPEC, **spec_overrides})
    report = validate(sql, spec, schema=SCHEMA)
    return report.verdict, {f.rule_id for f in report.findings}


def windowed(expr: str) -> str:
    return (
        f"WITH f AS (SELECT user_id, updated_at, {expr} AS v FROM user_stats) "
        f"{SEL}, f.v FROM labels l ASOF JOIN f ON f.user_id = l.user_id "
        "AND l.label_ts > f.updated_at"
    )


CLEAN = {
    # 1: FILTER on every aggregate that reads the table
    "filter on every aggregate": (
        f"{SEL}, SUM(e.amount) FILTER (WHERE e.ingested_at < l.label_ts) a, "
        f"COUNT(*) FILTER (WHERE e.ingested_at < l.label_ts) n {LEFT} {GROUP}"
    ),
    "filter in a correlated scalar subquery": (
        f"{SEL}, (SELECT SUM(e.amount) FILTER (WHERE e.ingested_at < l.label_ts) "
        "FROM events e WHERE e.user_id = l.user_id) a FROM labels l"
    ),
    "filter plus aggregates that ignore row counts": (
        f"{SEL}, SUM(e.amount) {BEFORE} a, COUNT(DISTINCT l.user_id) d, MAX(l.user_id) m "
        f"{LEFT} {GROUP}"
    ),
    "filter beside a window bound in the join": (
        f"{SEL}, SUM(e.amount) FILTER (WHERE e.event_ts >= l.label_ts - INTERVAL 7 DAY) a "
        f"FROM labels l LEFT JOIN events e ON e.user_id = l.user_id "
        f"AND e.ingested_at < l.label_ts {GROUP}"
    ),
    # 2: QUALIFY (HAVING MAX is a leak, see below)
    "having lower bound beside an ingestion bound": (
        f"{SEL}, SUM(e.amount) a {INNER} AND e.ingested_at < l.label_ts {GROUP} "
        "HAVING l.label_ts - INTERVAL 30 DAY < MIN(e.event_ts)"
    ),
    "bound only in qualify": (f"{SEL}, e.amount {INNER} QUALIFY e.ingested_at < l.label_ts"),
    # 3: spellings of the same inequality
    "difference compared with a zero interval": (
        f"{SEL}, SUM(e.amount) a {INNER} AND l.label_ts - e.ingested_at > INTERVAL 0 DAY {GROUP}"
    ),
    "difference compared with an interval": (
        f"{SEL}, SUM(e.amount) a {INNER} AND l.label_ts - e.ingested_at >= INTERVAL 1 HOUR {GROUP}"
    ),
    "difference on the right": (
        f"{SEL}, SUM(e.amount) a {INNER} AND INTERVAL 0 DAY < l.label_ts - e.ingested_at {GROUP}"
    ),
    "date_diff day above zero": (
        f"{SEL}, SUM(e.amount) a {INNER} "
        f"AND date_diff('day', e.ingested_at, l.label_ts) > 0 {GROUP}"
    ),
    "date_diff hour from one": (
        f"{SEL}, SUM(e.amount) a {INNER} "
        f"AND date_diff('hour', e.ingested_at, l.label_ts) >= 1 {GROUP}"
    ),
    "datediff upper form": (
        f"{SEL}, SUM(e.amount) a {INNER} "
        f"AND 0 < DATEDIFF('minute', e.ingested_at, l.label_ts) {GROUP}"
    ),
    "epoch of both sides": (
        f"{SEL}, SUM(e.amount) a {INNER} AND epoch(e.ingested_at) < epoch(l.label_ts) {GROUP}"
    ),
    "extract epoch on both sides": (
        f"{SEL}, SUM(e.amount) a {INNER} "
        f"AND EXTRACT(epoch FROM e.ingested_at) < EXTRACT(epoch FROM l.label_ts) {GROUP}"
    ),
    "epoch with a seconds margin": (
        f"{SEL}, SUM(e.amount) a {INNER} "
        f"AND epoch(e.ingested_at) < epoch(l.label_ts) - 3600 {GROUP}"
    ),
    "epoch difference in seconds": (
        f"{SEL}, SUM(e.amount) a {INNER} AND epoch(l.label_ts) - epoch(e.ingested_at) > 60 {GROUP}"
    ),
    # 4: a time split cut at a global label time
    "cutoff from the label times": f"{FEAT} WHERE l.label_ts < (SELECT MAX(label_ts) FROM labels)",
    "cutoff shifted back": (
        f"{FEAT} WHERE l.label_ts <= (SELECT MIN(label_ts) FROM labels) + INTERVAL 20 DAY"
    ),
    # 7: the earliest and per-group latest row
    "earliest row before the prediction": (
        f"{SEL}, s.total_spend FROM labels l JOIN user_stats s ON s.user_id = l.user_id "
        "WHERE s.updated_at = (SELECT MIN(x.updated_at) FROM user_stats x "
        "WHERE x.user_id = l.user_id AND x.updated_at < l.label_ts)"
    ),
    "latest per group before the prediction": (
        f"{SEL}, s.total_spend FROM labels l JOIN user_stats s ON s.user_id = l.user_id "
        "WHERE s.updated_at IN (SELECT MAX(x.updated_at) FROM user_stats x "
        "WHERE x.updated_at < l.label_ts GROUP BY x.user_id)"
    ),
    # 8: a CTE nothing reads
    "unused cte": (
        "WITH junk AS (SELECT SUM(amount) AS a FROM events), "
        "junk2 AS (SELECT * FROM junk) "
        f"{SEL}, u.country FROM labels l JOIN entities u ON u.user_id = l.user_id"
    ),
    "unused cte with an outcome table": (
        "WITH unused AS (SELECT user_id, refund_after_churn FROM user_outcomes) "
        f"{SEL}, u.country FROM labels l JOIN entities u ON u.user_id = l.user_id"
    ),
    # 9: a UNION inside a CTE or derived table
    "union spine in a cte": (
        "WITH sp AS (SELECT user_id, label_ts, churned FROM labels WHERE user_id < 100 "
        "UNION ALL SELECT user_id, label_ts, churned FROM labels WHERE user_id >= 100) "
        "SELECT sp.user_id, sp.label_ts, sp.churned, SUM(e.amount) a FROM sp "
        "LEFT JOIN events e ON e.user_id = sp.user_id AND e.ingested_at < sp.label_ts "
        "GROUP BY 1, 2, 3"
    ),
    "union spine in a derived table": (
        "SELECT sp.user_id, sp.label_ts, sp.churned, SUM(e.amount) a FROM "
        "(SELECT user_id, label_ts, churned FROM labels WHERE user_id < 100 "
        "UNION SELECT user_id, label_ts, churned FROM labels WHERE user_id >= 100) sp "
        "LEFT JOIN events e ON e.user_id = sp.user_id AND e.ingested_at < sp.label_ts "
        "GROUP BY 1, 2, 3"
    ),
    "union by name with the same columns in every branch": (
        "WITH ev AS (SELECT user_id, ingested_at, amount FROM events WHERE amount > 0 "
        "UNION ALL BY NAME SELECT amount, user_id, ingested_at FROM events WHERE amount <= 0) "
        f"{SEL}, SUM(ev.amount) a FROM labels l LEFT JOIN ev ON ev.user_id = l.user_id "
        f"AND ev.ingested_at < l.label_ts {GROUP}"
    ),
    "union of feature rows bounded after the union": (
        "WITH ev AS (SELECT user_id, ingested_at, amount FROM events WHERE amount > 0 "
        "UNION ALL SELECT user_id, ingested_at, amount FROM events WHERE amount <= 0) "
        f"{SEL}, SUM(ev.amount) a FROM labels l LEFT JOIN ev ON ev.user_id = l.user_id "
        f"AND ev.ingested_at < l.label_ts {GROUP}"
    ),
    # 10: LAG with a constant offset
    "lag with a constant offset": windowed(
        "LAG(total_spend, 2) OVER (PARTITION BY user_id ORDER BY updated_at)"
    ),
    # L7: the wall clock only chooses which prediction rows to train on
    "clock in a spine filter": f"{FEAT} WHERE l.label_ts < CURRENT_DATE - INTERVAL 30 DAY",
    "cutoff written the other way, upper bound": (
        f"{FEAT} WHERE (SELECT MAX(label_ts) FROM labels) > l.label_ts"
    ),
    "cutoff with a literal lower bound": (
        f"{FEAT} WHERE l.label_ts BETWEEN TIMESTAMP '2026-01-01' "
        "AND (SELECT MAX(label_ts) FROM labels)"
    ),
    "cutoff at a quantile as the start of the test window": (
        f"{FEAT} WHERE l.label_ts >= (SELECT quantile_cont(label_ts, 0.8) FROM labels)"
    ),
    "union by name with columns in another order": (
        "WITH ev AS (SELECT user_id, ingested_at AS t, amount FROM events UNION ALL BY NAME "
        "SELECT amount, user_id, ingested_at AS t FROM events) "
        f"{SEL}, SUM(ev.amount) a FROM labels l LEFT JOIN ev ON ev.user_id = l.user_id "
        f"AND ev.t < l.label_ts {GROUP}"
    ),
    "clock in a spine filter, now": f"{FEAT} AND l.label_ts <= now() - INTERVAL 1 DAY".replace(
        "AND", "WHERE", 1
    ),
}


@pytest.mark.parametrize("sql", CLEAN.values(), ids=CLEAN.keys())
def test_exempt_shapes_are_clean(sql):
    assert check(sql) == ("clean", set())


LEAKY = {
    # 1: the same shapes with one condition wrong
    "one aggregate without a filter": (
        f"{SEL}, COUNT(*) n, SUM(e.amount) FILTER (WHERE e.ingested_at < l.label_ts) a "
        f"{INNER} {GROUP}",
        "L3",
    ),
    "filter bound is inclusive": (
        f"{SEL}, SUM(e.amount) FILTER (WHERE e.ingested_at <= l.label_ts) a {LEFT} {GROUP}",
        "L5",
    ),
    "filter reads the future": (
        f"{SEL}, SUM(e.amount) FILTER (WHERE e.ingested_at >= l.label_ts) a {LEFT} {GROUP}",
        "L3",
    ),
    "filter on an inner join drops rows by the future": (
        f"{SEL}, SUM(e.amount) FILTER (WHERE e.ingested_at < l.label_ts) a {INNER} {GROUP}",
        "L3",
    ),
    "filter but the table is also grouped on": (
        f"{SEL}, e.event_ts, SUM(e.amount) FILTER (WHERE e.ingested_at < l.label_ts) a "
        f"{LEFT} GROUP BY 1, 2, 3, 4",
        "L3",
    ),
    "filter but a window reads the table": (
        f"{SEL}, SUM(e.amount) FILTER (WHERE e.ingested_at < l.label_ts) a, "
        f"SUM(e.amount) OVER (PARTITION BY l.user_id) w {LEFT} {GROUP.replace('3', '3, e.amount')}",
        "L3",
    ),
    "filter bounds a different aggregate": (
        f"{SEL}, SUM(e.amount) FILTER (WHERE e.ingested_at < l.label_ts) a, "
        f"MAX(e.event_ts) FILTER (WHERE e.amount > 0) b {LEFT} {GROUP}",
        "L3",
    ),
    # 2
    # the group is dropped when any of its rows is stamped later: which rows exist depends on
    # the future, so a HAVING on MAX / MIN of the time column is no bound
    "having max below the prediction": (
        f"{SEL}, SUM(e.amount) a {LEFT} {GROUP} HAVING MAX(e.ingested_at) < l.label_ts",
        "L3",
    ),
    "having max shifted back": (
        f"{SEL}, SUM(e.amount) a {INNER} {GROUP} "
        "HAVING MAX(e.ingested_at) <= l.label_ts - INTERVAL 1 SECOND",
        "L3",
    ),
    "having max is inclusive": (
        f"{SEL}, SUM(e.amount) a {LEFT} {GROUP} HAVING MAX(e.ingested_at) <= l.label_ts",
        "L3",
    ),
    "having min is not an upper bound": (
        f"{SEL}, SUM(e.amount) a {LEFT} {GROUP} HAVING MIN(e.ingested_at) < l.label_ts",
        "L3",
    ),
    "having max above the prediction": (
        f"{SEL}, SUM(e.amount) a {LEFT} {GROUP} HAVING MAX(e.ingested_at) > l.label_ts",
        "L3",
    ),
    "having min after the prediction": (
        f"{SEL}, SUM(e.amount) a {LEFT} {GROUP} HAVING MIN(e.ingested_at) > l.label_ts",
        "L3",
    ),
    "qualify bound is inclusive": (
        f"{SEL}, e.amount {INNER} QUALIFY e.ingested_at <= l.label_ts",
        "L5",
    ),
    "qualify bound with a window that saw every row": (
        f"{SEL}, s.total_spend FROM labels l JOIN user_stats s ON s.user_id = l.user_id "
        "QUALIFY ROW_NUMBER() OVER (PARTITION BY l.user_id, l.label_ts "
        "ORDER BY s.updated_at DESC) = 1 AND s.updated_at < l.label_ts",
        "L1",
    ),
    # 3
    "difference with a non-strict comparison": (
        f"{SEL}, SUM(e.amount) a {INNER} AND l.label_ts - e.ingested_at >= INTERVAL 0 DAY {GROUP}",
        "L5",
    ),
    "difference the wrong way round": (
        f"{SEL}, SUM(e.amount) a {INNER} AND l.label_ts - e.ingested_at < INTERVAL 0 DAY {GROUP}",
        "L1",
    ),
    "difference with a negative margin": (
        f"{SEL}, SUM(e.amount) a {INNER} "
        f"AND l.label_ts - e.ingested_at > INTERVAL '-2 days' {GROUP}",
        "L1",
    ),
    "date_diff admits the rest of the day": (
        f"{SEL}, SUM(e.amount) a {INNER} "
        f"AND date_diff('day', e.ingested_at, l.label_ts) >= 0 {GROUP}",
        "L1",
    ),
    "date_diff reversed": (
        f"{SEL}, SUM(e.amount) a {INNER} "
        f"AND date_diff('day', l.label_ts, e.ingested_at) > 0 {GROUP}",
        "L1",
    ),
    "date_diff in months is not read": (
        f"{SEL}, SUM(e.amount) a {INNER} "
        f"AND date_diff('month', e.ingested_at, l.label_ts) > 0 {GROUP}",
        "L3",
    ),
    "epoch non-strict": (
        f"{SEL}, SUM(e.amount) a {INNER} AND epoch(e.ingested_at) <= epoch(l.label_ts) {GROUP}",
        "L5",
    ),
    "epoch margin the wrong way": (
        f"{SEL}, SUM(e.amount) a {INNER} "
        f"AND epoch(e.ingested_at) < epoch(l.label_ts) + 3600 {GROUP}",
        "L5",
    ),
    "epoch difference reversed": (
        f"{SEL}, SUM(e.amount) a {INNER} AND epoch(e.ingested_at) - epoch(l.label_ts) > 60 {GROUP}",
        "L1",
    ),
    "difference in months has no exact length": (
        f"{SEL}, SUM(e.amount) a {INNER} AND l.label_ts - e.ingested_at > INTERVAL 1 MONTH {GROUP}",
        "L3",
    ),
    "bare number against a difference of timestamps": (
        f"{SEL}, SUM(e.amount) a {INNER} AND l.label_ts - e.ingested_at > 60 {GROUP}",
        "L3",
    ),
    # 4
    "cutoff used as a feature": (
        f"{SEL}, (SELECT MAX(label_ts) FROM labels) AS newest FROM labels l",
        "L3",
    ),
    "cutoff correlated with the row": (
        f"{FEAT} WHERE l.label_ts < (SELECT MAX(x.label_ts) FROM labels x "
        "WHERE x.user_id = l.user_id)",
        "L3",
    ),
    "cutoff chosen by the label": (
        f"{FEAT} WHERE l.label_ts < (SELECT MAX(x.label_ts) FROM labels x WHERE x.churned = 1)",
        "L6",
    ),
    "cutoff compared with a feature time": (
        f"{SEL}, SUM(e.amount) a {LEFT} AND e.ingested_at < (SELECT MAX(label_ts) FROM labels) "
        f"{GROUP}",
        "L3",
    ),
    # 5
    "prediction rank used as a feature": (
        f"{SEL}, ROW_NUMBER() OVER (PARTITION BY l.user_id ORDER BY l.label_ts DESC) AS r "
        "FROM labels l",
        "L1",
    ),
    # keeping only the entity's latest prediction: whether a row exists depends on later ones
    "latest prediction per entity": (
        f"{FEAT} QUALIFY ROW_NUMBER() OVER (PARTITION BY l.user_id ORDER BY l.label_ts DESC) = 1",
        "L1",
    ),
    "dedup ordered by the label": (
        f"{FEAT} QUALIFY ROW_NUMBER() OVER (PARTITION BY l.user_id "
        "ORDER BY l.churned DESC, l.label_ts) = 1",
        "L6",
    ),
    "dedup rank compared with a feature column": (
        f"{FEAT} QUALIFY ROW_NUMBER() OVER (PARTITION BY l.user_id "
        "ORDER BY l.label_ts DESC) = s.n_orders",
        "L1",
    ),
    # 6: still open, see the limitations
    "inline validity window in qualify": (
        f"{SEL}, s.total_spend FROM labels l JOIN user_stats s ON s.user_id = l.user_id "
        "QUALIFY l.label_ts > s.updated_at AND l.label_ts <= COALESCE(LEAD(s.updated_at) "
        "OVER (PARTITION BY s.user_id ORDER BY s.updated_at), TIMESTAMP '9999-12-31')",
        "L1",
    ),
    # 7
    "earliest row without the bound": (
        f"{SEL}, s.total_spend FROM labels l JOIN user_stats s ON s.user_id = l.user_id "
        "WHERE s.updated_at = (SELECT MIN(x.updated_at) FROM user_stats x "
        "WHERE x.user_id = l.user_id)",
        "L2",
    ),
    "earliest row with an inclusive bound": (
        f"{SEL}, s.total_spend FROM labels l JOIN user_stats s ON s.user_id = l.user_id "
        "WHERE s.updated_at = (SELECT MIN(x.updated_at) FROM user_stats x "
        "WHERE x.user_id = l.user_id AND x.updated_at <= l.label_ts)",
        "L5",
    ),
    "per-group latest without the bound": (
        f"{SEL}, s.total_spend FROM labels l JOIN user_stats s ON s.user_id = l.user_id "
        "WHERE s.updated_at IN (SELECT MAX(x.updated_at) FROM user_stats x GROUP BY x.user_id)",
        "L2",
    ),
    "not in the per-group latest": (
        f"{SEL}, s.total_spend FROM labels l JOIN user_stats s ON s.user_id = l.user_id "
        "WHERE s.updated_at NOT IN (SELECT MAX(x.updated_at) FROM user_stats x "
        "WHERE x.updated_at < l.label_ts GROUP BY x.user_id)",
        "L2",
    ),
    # 8
    "cte that is read": (
        "WITH tot AS (SELECT user_id, SUM(amount) AS a FROM events GROUP BY user_id) "
        f"{SEL}, tot.a FROM labels l JOIN tot ON tot.user_id = l.user_id",
        "L3",
    ),
    "cte read only by another read cte": (
        "WITH a AS (SELECT user_id, SUM(amount) AS s FROM events GROUP BY user_id), "
        "b AS (SELECT user_id, s FROM a) "
        f"{SEL}, b.s FROM labels l JOIN b ON b.user_id = l.user_id",
        "L3",
    ),
    # 9
    "union spine, features unbounded": (
        "WITH sp AS (SELECT user_id, label_ts, churned FROM labels WHERE user_id < 100 "
        "UNION ALL SELECT user_id, label_ts, churned FROM labels WHERE user_id >= 100) "
        "SELECT sp.user_id, sp.label_ts, sp.churned, SUM(e.amount) a FROM sp "
        "LEFT JOIN events e ON e.user_id = sp.user_id GROUP BY 1, 2, 3",
        "L3",
    ),
    "union spine, bound inclusive": (
        "WITH sp AS (SELECT user_id, label_ts, churned FROM labels WHERE user_id < 100 "
        "UNION ALL SELECT user_id, label_ts, churned FROM labels WHERE user_id >= 100) "
        "SELECT sp.user_id, sp.label_ts, sp.churned, SUM(e.amount) a FROM sp "
        "LEFT JOIN events e ON e.user_id = sp.user_id AND e.ingested_at <= sp.label_ts "
        "GROUP BY 1, 2, 3",
        "L5",
    ),
    "union of feature rows, bound missing": (
        "WITH ev AS (SELECT user_id, ingested_at, amount FROM events WHERE amount > 0 "
        "UNION ALL SELECT user_id, ingested_at, amount FROM events WHERE amount <= 0) "
        f"{SEL}, SUM(ev.amount) a FROM labels l LEFT JOIN ev ON ev.user_id = l.user_id {GROUP}",
        "L2",
    ),
    # a joined row still counts when no column of the table is named
    "filter beside COUNT(1)": (f"{SEL}, SUM(e.amount) {BEFORE} a, COUNT(1) n {LEFT} {GROUP}", "L3"),
    "filter beside SUM(1)": (f"{SEL}, SUM(e.amount) {BEFORE} a, SUM(1) n {LEFT} {GROUP}", "L3"),
    "filter beside COUNT of a spine column": (
        f"{SEL}, SUM(e.amount) {BEFORE} a, COUNT(l.user_id) n {LEFT} {GROUP}",
        "L3",
    ),
    "filter beside a list of a spine column": (
        f"{SEL}, SUM(e.amount) {BEFORE} a, LEN(LIST(l.user_id)) n {LEFT} {GROUP}",
        "L3",
    ),
    "filter beside HAVING COUNT(1)": (
        f"{SEL}, SUM(e.amount) {BEFORE} a {LEFT} {GROUP} HAVING COUNT(1) > 3",
        "L3",
    ),
    "filter in a subquery beside COUNT(1)": (
        f"{SEL}, (SELECT SUM(e.amount) {BEFORE} / COUNT(1) FROM events e "
        "WHERE e.user_id = l.user_id) a FROM labels l",
        "L3",
    ),
    "filter beside a window over the joined rows": (
        f"{SEL}, SUM(e.amount) {BEFORE} a, COUNT(*) OVER (PARTITION BY l.user_id, l.label_ts) n "
        f"{LEFT} {GROUP}",
        "L3",
    ),
    # a WHERE on the joined side decides which spine rows exist
    "filter and a value predicate in WHERE": (
        f"{SEL}, SUM(e.amount) {BEFORE} a {LEFT} WHERE e.amount > 50 {GROUP}",
        "L3",
    ),
    "filter and a not-null test in WHERE": (
        f"{SEL}, SUM(e.amount) {BEFORE} a {LEFT} WHERE e.event_id IS NOT NULL {GROUP}",
        "L3",
    ),
    "filter and the join condition moved to WHERE": (
        f"{SEL}, COUNT(e.event_id) {BEFORE} n {LEFT} WHERE e.user_id = l.user_id {GROUP}",
        "L3",
    ),
    # an aggregate the analysis does not know may count the joined rows too
    **{
        f"filter beside {fn}": (f"{SEL}, SUM(e.amount) {BEFORE} a, {fn} n {LEFT} {GROUP}", "L3")
        for fn in (
            "count_star()",
            "fsum(1)",
            "sumkahan(1)",
            "kahan_sum(1)",
            "product(2)",
            "histogram(l.user_id)",
            "favg(l.user_id)",
        )
    },
    "filter beside an unknown window function": (
        f"{SEL}, SUM(e.amount) {BEFORE} a, fsum(l.user_id) OVER (PARTITION BY l.label_ts) n "
        f"{LEFT} {GROUP}",
        "L3",
    ),
    # a UNION BY NAME lines columns up by name, not position
    "union by name with the bounded column in another position": (
        "WITH ev AS (SELECT user_id, ingested_at AS t, amount FROM events UNION ALL BY NAME "
        "SELECT user_id, ingested_at AS x, amount, ingested_at - INTERVAL 30 DAY AS t FROM events) "
        f"{SEL}, SUM(ev.amount) a FROM labels l LEFT JOIN ev ON ev.user_id = l.user_id "
        f"AND ev.t < l.label_ts {GROUP}",
        "L1",
    ),
    "union by name where a branch lacks the column": (
        "WITH ev AS (SELECT user_id, ingested_at AS t, amount FROM events UNION BY NAME "
        "SELECT user_id, ingested_at AS x, amount FROM events) "
        f"{SEL}, SUM(ev.amount) a FROM labels l LEFT JOIN ev ON ev.user_id = l.user_id "
        f"AND ev.t < l.label_ts {GROUP}",
        "L2",
    ),
    "label hidden in a column the other branch names differently (cte)": (
        "WITH s AS (SELECT user_id, label_ts, churned, 0 AS f FROM labels UNION ALL BY NAME "
        "SELECT user_id, label_ts, churned AS f, 0 AS churned FROM labels) "
        "SELECT s.user_id, s.label_ts, s.churned, s.f FROM s",
        "L6",
    ),
    "label hidden in a column the other branch names differently (root)": (
        "SELECT user_id, label_ts, churned, 0 AS f FROM labels UNION ALL BY NAME "
        "SELECT user_id, label_ts, churned AS f, 0 AS churned FROM labels",
        "L6",
    ),
    # a window that skips nulls still reads the next row
    "lead ignoring nulls": (
        windowed("LEAD(total_spend IGNORE NULLS) OVER (PARTITION BY user_id ORDER BY updated_at)"),
        "L1",
    ),
    "lag backwards ignoring nulls": (
        windowed(
            "LAG(total_spend, -1) IGNORE NULLS OVER (PARTITION BY user_id ORDER BY updated_at)"
        ),
        "L1",
    ),
    # a time split that keeps only the latest prediction
    "cutoff equal to the latest label time": (
        f"{FEAT} WHERE l.label_ts = (SELECT MAX(label_ts) FROM labels)",
        "L3",
    ),
    "cutoff at or after the latest label time": (
        f"{FEAT} WHERE l.label_ts >= (SELECT MAX(label_ts) FROM labels)",
        "L3",
    ),
    "cutoff after the latest label time": (
        f"{FEAT} WHERE l.label_ts > (SELECT MAX(label_ts) FROM labels)",
        "L3",
    ),
    "cutoff written the other way round": (
        f"{FEAT} WHERE (SELECT MAX(label_ts) FROM labels) <= l.label_ts",
        "L3",
    ),
    "between the latest label time and itself": (
        f"{FEAT} WHERE l.label_ts BETWEEN (SELECT MAX(label_ts) FROM labels) "
        "AND (SELECT MAX(label_ts) FROM labels)",
        "L3",
    ),
    "cutoff from an aggregate that is not a time statistic": (
        f"{FEAT} WHERE l.user_id < (SELECT COUNT(label_ts) FROM labels)",
        "L3",
    ),
    # 10
    "lag with a column offset": (
        windowed("LAG(total_spend, n_orders) OVER (PARTITION BY user_id ORDER BY updated_at)"),
        "L1",
    ),
    "lag with a negative offset": (
        windowed("LAG(total_spend, -1) OVER (PARTITION BY user_id ORDER BY updated_at)"),
        "L1",
    ),
    # L7
    "days since the last event as of today": (
        f"{SEL}, date_diff('day', MAX(e.event_ts), current_date) AS recency "
        f"FROM labels l JOIN events e ON e.user_id = l.user_id AND e.ingested_at < l.label_ts "
        f"{GROUP}",
        "L7",
    ),
    "now in a projection": (f"{FEAT.replace('s.total_spend', 'now() AS stamp')}", "L7"),
    "current_timestamp in a bound on a feature table": (
        f"{SEL}, SUM(e.amount) a {LEFT} AND e.ingested_at < l.label_ts "
        f"AND e.event_ts <= current_timestamp {GROUP}",
        "L7",
    ),
    "get_current_timestamp": (
        f"{SEL}, date_diff('day', MAX(e.ingested_at), get_current_timestamp()) r {LEFT} "
        f"AND e.ingested_at < l.label_ts {GROUP}",
        "L7",
    ),
    "transaction_timestamp": (
        f"{SEL}, date_diff('day', MAX(e.ingested_at), transaction_timestamp()) r {LEFT} "
        f"AND e.ingested_at < l.label_ts {GROUP}",
        "L7",
    ),
    "age of a timestamp as of today": (
        f"{SEL}, age(MAX(e.ingested_at)) r {LEFT} AND e.ingested_at < l.label_ts {GROUP}",
        "L7",
    ),
    "clock inside a function the analysis does not know, in a spine filter": (
        f"{FEAT} WHERE l.label_ts < CAST(strftime(now(), '%Y-%m-%d') AS TIMESTAMP)",
        "L7",
    ),
    "current_localtime": (f"{SEL}, current_localtime() AS built FROM labels l", "L7"),
    "unix_timestamp without an argument": (f"{SEL}, unix_timestamp() AS built FROM labels l", "L7"),
    "clock in the join condition on the spine side": (
        f"{SEL}, SUM(e.amount) a {LEFT} AND e.ingested_at < l.label_ts "
        f"AND l.label_ts > now() - INTERVAL 30 DAY {GROUP}",
        "L7",
    ),
    "clock in the filter of a feature subquery": (
        f"{SEL}, (SELECT SUM(e.amount) FROM events e WHERE e.user_id = l.user_id "
        "AND e.ingested_at < l.label_ts AND l.label_ts > now() - INTERVAL 30 DAY) a FROM labels l",
        "L7",
    ),
    "getdate": (f"{SEL}, getdate() AS built_at FROM labels l", "L7"),
    "clock mixed with a feature column in a filter": (
        f"{FEAT} WHERE l.label_ts < CURRENT_DATE AND s.updated_at < CURRENT_DATE",
        "L7",
    ),
    "clock in a case expression": (
        f"{SEL}, CASE WHEN l.label_ts < now() THEN 1 ELSE 0 END AS old FROM labels l",
        "L7",
    ),
}


@pytest.mark.parametrize("sql, rule", LEAKY.values(), ids=LEAKY.keys())
def test_wrong_condition_stays_leaky(sql, rule):
    verdict, rules = check(sql)
    assert verdict == "leaky" and rule in rules, (verdict, rules)


def test_union_branch_that_is_not_a_prediction_time_is_not_clean():
    sql = (
        "WITH sp AS (SELECT user_id, label_ts, churned FROM labels "
        "UNION ALL SELECT user_id, TIMESTAMP '2030-01-01' AS label_ts, churned FROM labels) "
        "SELECT sp.user_id, sp.label_ts, sp.churned, SUM(e.amount) a FROM sp "
        "LEFT JOIN events e ON e.user_id = sp.user_id AND e.ingested_at < sp.label_ts "
        "GROUP BY 1, 2, 3"
    )
    verdict, rules = check(sql)
    assert verdict != "clean" and "SPEC" in rules


# --- spec fields ---------------------------------------------------------------------------

EVENT_LAG = (
    f"{SEL}, SUM(e.amount) a {LEFT} AND e.event_ts < l.label_ts - INTERVAL {{lag}} "
    "AND e.event_ts >= l.label_ts - INTERVAL 90 DAY GROUP BY 1, 2, 3"
)
DELAY = {"max_ingest_delay": {"events": "2 days"}}


@pytest.mark.parametrize("lag", ["2 DAY", "3 DAY", "48 HOUR", "'3 days'", "1 WEEK"])
def test_event_time_lag_at_least_the_declared_delay_needs_no_ingestion_bound(lag):
    sql = EVENT_LAG.format(lag=lag)
    assert check(sql)[1] == {"L4"}  # undeclared: the hint stays
    assert check(sql, **DELAY) == ("clean", set())


@pytest.mark.parametrize("lag", ["1 DAY", "47 HOUR", "'1 day 23 hours'", "'5'"])
def test_event_time_lag_shorter_than_the_delay_keeps_the_hint(lag):
    # a unitless INTERVAL '5' is five seconds in DuckDB, never "5 days"
    assert check(EVENT_LAG.format(lag=lag), **DELAY)[1] == {"L4"}


def test_a_month_lag_counts_at_its_shortest_length():
    sql = EVENT_LAG.format(lag="1 MONTH")
    assert check(sql, max_ingest_delay={"events": "27 days"}) == ("clean", set())
    assert check(sql, max_ingest_delay={"events": "29 days"})[1] == {"L4"}


def test_delay_boundary_is_strict_unless_the_spec_is_inclusive():
    exact = EVENT_LAG.replace("e.event_ts <", "e.event_ts <=", 1).format(lag="2 DAY")
    assert check(exact, **DELAY)[1] == {"L4"}
    assert check(exact, **DELAY, boundary="inclusive") == ("clean", set())


def test_availability_bound_still_wins_when_a_delay_is_declared():
    sql = f"{SEL}, SUM(e.amount) a {LEFT} AND e.ingested_at <= l.label_ts - INTERVAL 1 HOUR {GROUP}"
    assert check(sql, **DELAY) == check(sql) == ("clean", set())


def test_inclusive_boundary_accepts_a_bound_at_the_prediction_time():
    inclusive = f"{SEL}, SUM(e.amount) a {LEFT} AND e.ingested_at <= l.label_ts {GROUP}"
    assert check(inclusive)[1] == {"L5"}
    assert check(inclusive, boundary="inclusive") == ("clean", set())
    ahead = inclusive.replace("<= l.label_ts", "<= l.label_ts + INTERVAL 6 HOUR")
    assert check(ahead, boundary="inclusive")[0] == "leaky"  # a shift forward is still a leak


MATURED = (
    "SELECT sp.user_id, sp.label_ts, sp.churned, (SELECT AVG(h.churned) FROM labels h "
    "JOIN entities he ON he.user_id = h.user_id WHERE he.country = u.country "
    "AND h.label_ts <= sp.label_ts - INTERVAL {mature} DAY) AS rate "
    "FROM labels sp JOIN entities u ON u.user_id = sp.user_id"
)
KNOWN = {"label_known_after": "14 days"}


@pytest.mark.parametrize("op", ["<", "<="])
@pytest.mark.parametrize("mature", [15, 21, 30])
def test_target_encoding_over_matured_labels_is_clean(mature, op):
    sql = MATURED.format(mature=mature).replace("<=", op)
    assert check(sql)[0] == "leaky"  # without the declaration the labels are the outcome
    assert check(sql, **KNOWN) == ("clean", set())


def test_maturity_follows_the_boundary_option():
    """A label stamped exactly `label_known_after` before the prediction is final only when the
    spec says facts at the prediction time count (`<` is fine either way)."""
    at_horizon = MATURED.format(mature=14)
    assert check(at_horizon, **KNOWN)[0] == "leaky"
    assert check(at_horizon, **KNOWN, boundary="inclusive") == ("clean", set())
    assert check(at_horizon.replace("<=", "<"), **KNOWN) == ("clean", set())


@pytest.mark.parametrize("mature", [1, 7, 13])
def test_target_encoding_over_unmatured_labels_stays_leaky(mature):
    verdict, rules = check(MATURED.format(mature=mature), **KNOWN)
    assert verdict == "leaky" and "L6" in rules


def test_matured_labels_do_not_excuse_the_spine_label():
    sql = MATURED.format(mature=21).replace("AS rate", "AS rate, sp.churned AS copy")
    verdict, rules = check(sql, **KNOWN)
    assert verdict == "leaky" and "L6" in rules
    mixed = MATURED.format(mature=21).replace(
        "(SELECT AVG(h.churned)", "(SELECT AVG(h.churned) + sp.churned"
    )
    assert check(mixed, **KNOWN)[0] == "leaky"


def test_a_month_back_counts_at_its_shortest_length_for_label_maturity():
    sql = MATURED.replace("INTERVAL {mature} DAY", "INTERVAL 1 MONTH")
    assert check(sql, label_known_after="27 days") == ("clean", set())
    assert check(sql, label_known_after="29 days")[0] == "leaky"


def test_labels_are_matured_only_along_every_use():
    sql = (
        f"{SEL}, (SELECT AVG(h.churned) FROM labels h WHERE h.user_id = l.user_id "
        "AND h.label_ts <= l.label_ts - INTERVAL 30 DAY) a, "
        "(SELECT AVG(h.churned) FROM labels h WHERE h.user_id = l.user_id "
        "AND h.label_ts <= l.label_ts - INTERVAL 2 DAY) b FROM labels l"
    )
    assert check(sql, **KNOWN)[0] == "leaky"


@pytest.mark.parametrize(
    "extra, message",
    [
        ({"boundary": "sloppy"}, "boundary"),
        ({"max_ingest_delay": {"events": "soon"}}, "duration"),
        ({"max_ingest_delay": {"events": "0 days"}}, "duration"),
        ({"max_ingest_delay": {"events": "2 months"}}, "duration"),
        ({"max_ingest_delay": {"events": 2}}, "duration"),
        ({"max_ingest_delay": {"entities": "2 days"}}, "time column"),
        ({"max_ingest_delay": ["events"]}, "mapping"),
        ({"label_known_after": "-1 days"}, "duration"),
        ({"relations": {"a.b.c.d": "events"}}, "one to three"),
    ],
)
def test_bad_new_fields_are_rejected(extra, message):
    with pytest.raises(SpecError, match=message):
        LeakageSpec.from_dict({**SPEC, **extra})


def test_new_fields_parse_and_default_to_v1():
    spec = LeakageSpec.from_dict(SPEC)
    assert (spec.boundary, spec.max_ingest_delay, spec.label_known_after, spec.relations) == (
        "strict",
        {},
        None,
        {},
    )
    full = LeakageSpec.from_dict(
        {
            **SPEC,
            "boundary": "Inclusive",
            "max_ingest_delay": {"Events": "2 days", "user_stats": "36 hours"},
            "label_known_after": "2 weeks",
            "relations": {'"DB"."main"."Events"': "events", "analytics.events": "events"},
        }
    )
    assert full.boundary == "inclusive"
    assert full.max_ingest_delay == {"events": 172_800.0, "user_stats": 129_600.0}
    assert full.label_known_after == 1_209_600.0
    assert full.relations == {"db.main.events": "events", "analytics.events": "events"}


SELF_CTE = (
    "WITH h AS (SELECT user_id, label_ts, churned FROM labels) "
    "SELECT a.user_id, a.label_ts, a.churned, {extra} AVG(b.churned) te "
    "FROM h a LEFT JOIN h b ON b.user_id = a.user_id "
    "AND b.label_ts < a.label_ts - INTERVAL 15 DAY GROUP BY a.user_id, a.label_ts, a.churned{group}"
)


@pytest.mark.parametrize(
    "sql",
    [
        SELF_CTE.format(extra="a.churned + 0 AS label_copy,", group=", label_copy"),
        SELF_CTE.format(extra="", group="").replace(
            "AVG(b.churned) te", "AVG(b.churned) + a.churned AS te"
        ),
    ],
    ids=["copy of the spine label", "spine label added to the encoding"],
)
def test_one_label_cte_as_spine_and_as_matured_history_keeps_the_target_a_leak(sql):
    """The reference that is the spine is the target whatever else reads it as history."""
    verdict, rules = check(sql, **KNOWN)
    assert verdict == "leaky" and "L6" in rules
    assert check(sql)[0] == "leaky"


def test_history_from_a_separate_label_reference_is_still_matured():
    sql = (
        "SELECT a.user_id, a.label_ts, a.churned, AVG(b.churned) te "
        "FROM labels a LEFT JOIN labels b ON b.user_id = a.user_id "
        "AND b.label_ts < a.label_ts - INTERVAL 15 DAY GROUP BY a.user_id, a.label_ts, a.churned"
    )
    assert check(sql, **KNOWN) == ("clean", set())


def test_relations_keys_are_qualified_names():
    with pytest.raises(SpecError, match="bare name"):
        LeakageSpec.from_dict({**SPEC, "relations": {"events": "events"}})


def test_union_by_name_over_an_unnamed_column_is_refused_not_guessed():
    sql = (
        "WITH ev AS (SELECT user_id, ingested_at + INTERVAL 0 DAY, amount FROM events "
        "UNION ALL BY NAME SELECT user_id, ingested_at, amount FROM events) "
        f"{SEL}, SUM(ev.amount) a FROM labels l LEFT JOIN ev ON ev.user_id = l.user_id {GROUP}"
    )
    verdict, rules = check(sql)
    assert verdict == "unverifiable" and rules == {"PARSE"}
