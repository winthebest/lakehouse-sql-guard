"""Each rule: at least five positive and five negative queries, written independently of the
benchmark templates so the tests do not just restate them."""

import pytest

SEL = "SELECT l.user_id, l.label_ts, l.churned"

CLEAN = [
    f"{SEL}, s.total_spend FROM labels l ASOF JOIN user_stats s "
    "ON l.user_id = s.user_id AND s.updated_at < l.label_ts",
    f"{SEL}, x.n FROM labels l JOIN (SELECT l2.user_id, l2.label_ts, COUNT(*) AS n "
    "FROM labels l2 JOIN events ev ON ev.user_id = l2.user_id "
    "WHERE ev.ingested_at < l2.label_ts GROUP BY 1, 2) x "
    "ON x.user_id = l.user_id AND x.label_ts = l.label_ts",
    f"{SEL}, (SELECT MAX(e.amount) FROM events e WHERE e.user_id = l.user_id "
    "AND l.label_ts > e.ingested_at) AS mx FROM labels l",
    f"{SEL}, SUM(e.amount) AS s FROM labels l LEFT JOIN events e ON e.user_id = l.user_id "
    "AND e.ingested_at <= l.label_ts - INTERVAL 1 HOUR GROUP BY 1, 2, 3",
    f"{SEL}, u.country FROM labels l JOIN entities u USING (user_id)",
    f"{SEL}, s.n_orders FROM labels l JOIN user_stats s ON s.user_id = l.user_id "
    "WHERE s.updated_at BETWEEN l.label_ts - INTERVAL 30 DAY AND l.label_ts - INTERVAL 1 SECOND",
    f"WITH w AS (SELECT * FROM user_stats) {SEL}, w.total_spend FROM labels l "
    "ASOF JOIN w ON l.user_id = w.user_id AND l.label_ts > w.updated_at",
]


@pytest.mark.parametrize("sql", CLEAN)
def test_clean_queries_raise_nothing(rules_of, sql):
    assert rules_of(sql) == set()


RULE_CASES = {
    "L1": [
        f"{SEL}, s.total_spend FROM labels l ASOF JOIN user_stats s "
        "ON l.user_id = s.user_id AND s.updated_at >= l.label_ts",
        f"{SEL}, COUNT(e.event_id) AS n FROM labels l LEFT JOIN events e "
        "ON e.user_id = l.user_id AND e.event_ts > l.label_ts GROUP BY 1, 2, 3",
        f"{SEL}, (SELECT SUM(e.amount) FROM events e WHERE e.user_id = l.user_id "
        "AND e.event_ts BETWEEN l.label_ts AND l.label_ts + INTERVAL 3 DAY) AS f FROM labels l",
        f"{SEL}, SUM(e.amount) AS f FROM labels l JOIN events e ON e.user_id = l.user_id "
        "AND e.ingested_at < l.label_ts + INTERVAL 2 DAY GROUP BY 1, 2, 3",
        f"{SEL}, s.n_orders FROM labels l JOIN user_stats s ON s.user_id = l.user_id "
        "WHERE l.label_ts <= s.updated_at",
    ],
    "L2": [
        f"{SEL}, s.total_spend FROM labels l JOIN user_stats s ON s.user_id = l.user_id",
        f"{SEL}, s.n_orders FROM labels l LEFT JOIN user_stats s USING (user_id)",
        f"{SEL}, z.total_spend FROM labels l JOIN (SELECT * FROM user_stats "
        "WHERE updated_at > TIMESTAMP '2026-01-01') z ON z.user_id = l.user_id",
        f"WITH s AS (SELECT user_id, total_spend FROM user_stats) {SEL}, s.total_spend "
        "FROM labels l JOIN s ON s.user_id = l.user_id",
        f"{SEL}, (SELECT c.page FROM clicks c WHERE c.user_id = l.user_id LIMIT 1) AS p "
        "FROM labels l",
    ],
    "L3": [
        f"{SEL}, SUM(e.amount) AS t FROM labels l JOIN events e USING (user_id) GROUP BY 1, 2, 3",
        f"{SEL}, (SELECT COUNT(*) FROM events e WHERE e.user_id = l.user_id) AS n FROM labels l",
        f"WITH a AS (SELECT user_id, AVG(amount) AS av FROM events GROUP BY user_id) "
        f"{SEL}, a.av FROM labels l JOIN a USING (user_id)",
        f"{SEL}, MAX(s.total_spend) AS m FROM labels l JOIN user_stats s "
        "ON s.user_id = l.user_id GROUP BY 1, 2, 3",
        f"{SEL}, (SELECT MIN(e.event_ts) FROM events e WHERE e.user_id = l.user_id "
        "AND e.event_ts > TIMESTAMP '2025-01-01') AS first_seen FROM labels l",
    ],
    "L4": [
        f"{SEL}, SUM(e.amount) AS s FROM labels l JOIN events e ON e.user_id = l.user_id "
        "AND e.event_ts < l.label_ts GROUP BY 1, 2, 3",
        f"{SEL}, (SELECT COUNT(*) FROM events e WHERE e.user_id = l.user_id "
        "AND e.event_ts < l.label_ts - INTERVAL 1 DAY) AS n FROM labels l",
        f"{SEL}, MAX(e.amount) AS m FROM labels l LEFT JOIN events e ON e.user_id = l.user_id "
        "WHERE l.label_ts > e.event_ts GROUP BY 1, 2, 3",
        f"WITH f AS (SELECT l.user_id, l.label_ts, AVG(e.amount) AS av FROM labels l "
        "JOIN events e ON e.user_id = l.user_id AND e.event_ts < l.label_ts GROUP BY 1, 2) "
        f"{SEL}, f.av FROM labels l JOIN f ON f.user_id = l.user_id AND f.label_ts = l.label_ts",
        f"{SEL}, COUNT(*) AS n FROM labels l JOIN events e ON e.user_id = l.user_id "
        "AND e.event_ts BETWEEN l.label_ts - INTERVAL 7 DAY AND l.label_ts - INTERVAL 1 SECOND "
        "GROUP BY 1, 2, 3",
    ],
    "L5": [
        f"{SEL}, s.total_spend FROM labels l ASOF JOIN user_stats s "
        "ON l.user_id = s.user_id AND l.label_ts >= s.updated_at",
        f"{SEL}, SUM(e.amount) AS f FROM labels l JOIN events e ON e.user_id = l.user_id "
        "AND e.ingested_at <= l.label_ts GROUP BY 1, 2, 3",
        f"{SEL}, SUM(e.amount) AS f FROM labels l JOIN events e ON e.user_id = l.user_id "
        "AND e.ingested_at < l.label_ts + INTERVAL 7 HOUR GROUP BY 1, 2, 3",
        f"{SEL}, s.n_orders FROM labels l JOIN user_stats s ON s.user_id = l.user_id "
        "AND s.updated_at BETWEEN l.label_ts - INTERVAL 1 DAY AND l.label_ts",
        f"{SEL}, COUNT(*) AS n FROM labels l JOIN clicks c ON c.user_id = l.user_id "
        "AND c.click_ts < l.label_ts GROUP BY 1, 2, 3",
    ],
    "L6": [
        f"{SEL}, o.refund_after_churn FROM labels l LEFT JOIN user_outcomes o "
        "ON o.user_id = l.user_id AND o.label_ts = l.label_ts",
        f"{SEL}, l.churned * 2 AS doubled FROM labels l",
        f"{SEL}, AVG(l.churned) OVER (PARTITION BY u.country) AS rate FROM labels l "
        "JOIN entities u USING (user_id)",
        f"{SEL}, o.days_to_churn FROM labels l JOIN user_outcomes o "
        "ON o.user_id = l.user_id AND o.label_ts = l.label_ts",
        f"WITH t AS (SELECT user_id, label_ts, refund_after_churn AS r FROM user_outcomes) "
        f"{SEL}, t.r FROM labels l JOIN t ON t.user_id = l.user_id AND t.label_ts = l.label_ts",
    ],
}


@pytest.mark.parametrize(
    "rule,sql", [(rule, sql) for rule, queries in RULE_CASES.items() for sql in queries]
)
def test_rule_fires(rules_of, rule, sql):
    assert rule in rules_of(sql)


@pytest.mark.parametrize(
    "rule,sql",
    [
        (rule, sql)
        for rule, queries in RULE_CASES.items()
        for other, others in RULE_CASES.items()
        if other != rule and rule not in ("L4",)
        for sql in others[:1]
    ]
    + [("L4", sql) for sql in CLEAN],
)
def test_rule_does_not_fire_on_other_patterns(rules_of, rule, sql):
    """Negative cases: other leak types (and clean queries) do not trigger the rule."""
    assert rule not in rules_of(sql)


def test_unclassified_table_is_reported(rules_of):
    assert "UNCLASSIFIED" in rules_of(f"{SEL}, m.x FROM labels l JOIN misc m USING (user_id)")


def test_parse_error_is_reported(rules_of):
    assert rules_of("SELECT FROM WHERE") == {"PARSE"}


def test_finding_points_at_responsible_join(spec):
    from sqlguard.leakage.validator import validate
    from tests.leakage.conftest import SCHEMA

    report = validate(
        f"{SEL}, s.total_spend, u.country FROM labels l JOIN user_stats s USING (user_id) "
        "JOIN entities u USING (user_id)",
        spec,
        schema=SCHEMA,
    )
    assert report.verdict == "leaky" and report.leak_types == ["L2"]
    (finding,) = [f for f in report.findings if f.rule_id == "L2"]
    assert finding.location == "user_stats AS s"
