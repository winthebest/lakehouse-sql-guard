"""Queries that bound or order time in a way the checker did not follow, and the same queries
with a wrong bound.

Every shape is a pair: the correct query must not be flagged, and its twin, which uses the same
syntax with a bound that is wrong (or missing, or ambiguous), must still be flagged. The twin is
what keeps a fix from turning into a blanket exemption."""

import pytest

from sqlguard.leakage.spec import LeakageSpec
from sqlguard.leakage.validator import static_findings

from .conftest import SCHEMA, SPEC

KEYS = "l.user_id, l.label_ts, l.churned"


def spec_with(**overrides) -> LeakageSpec:
    return LeakageSpec.from_dict({**SPEC, **overrides})


def rules(sql: str, **overrides) -> set[str]:
    return {f.rule_id for f in static_findings(sql, spec_with(**overrides), SCHEMA)}


def assert_pair(clean: str, leaky: str, rule: str | None = None, **overrides) -> None:
    assert rules(clean, **overrides) == set(), "the correct query is flagged"
    got = rules(leaky, **overrides)
    assert got, "the leaky twin is clean"
    if rule is not None:
        assert rule in got


# --- time read as a number since 1970 ------------------------------------------------------------

SPARK = {"dialect": "spark"}


def window_over(order: str, frame: str = "RANGE BETWEEN 3600 PRECEDING AND 1 PRECEDING") -> str:
    return f"""WITH txns AS (SELECT user_id, label_ts, churned, CAST(label_ts AS BIGINT) AS ts_unix,
    CAST(churned AS BIGINT) AS other FROM labels)
SELECT user_id, label_ts, churned,
  COUNT(user_id) OVER (PARTITION BY user_id ORDER BY {order} {frame}) AS n_1h
FROM txns"""


@pytest.mark.parametrize("dialect", ["spark", "databricks"])
def test_a_window_ordered_by_the_epoch_of_the_prediction_time_looks_back_only(dialect):
    assert_pair(
        window_over("ts_unix"),
        window_over("ts_unix DESC"),
        "L1",
        dialect=dialect,
    )


def test_an_epoch_ordered_window_that_looks_forward_is_still_flagged():
    clean = window_over("ts_unix")
    leaky = window_over("ts_unix", "RANGE BETWEEN 3600 PRECEDING AND 600 FOLLOWING")
    assert_pair(clean, leaky, "L1", **SPARK)


def test_a_window_ordered_by_the_cast_of_another_column_is_still_flagged():
    assert_pair(window_over("ts_unix"), window_over("other"), "L1", **SPARK)


def test_a_cast_to_an_integer_orders_rows_in_every_dialect_but_bounds_only_in_spark():
    # the cast keeps the order of the timestamp everywhere, so the window is fine; whether the
    # number is seconds is known only for Spark and Databricks
    for dialect in ("duckdb", "trino", "snowflake"):
        assert_pair(window_over("ts_unix"), window_over("ts_unix DESC"), "L1", dialect=dialect)
        shifted = bounded_by("CAST(s.updated_at AS BIGINT)", "CAST(l.label_ts AS BIGINT)")
        assert rules(shifted, dialect=dialect) != set(), dialect


def bounded_by(feature: str, label: str) -> str:
    return f"""SELECT {KEYS}, SUM(s.total_spend) AS spend
FROM labels l LEFT JOIN user_stats s ON s.user_id = l.user_id AND {feature} < {label}
GROUP BY 1, 2, 3"""


def test_a_bound_written_on_epoch_numbers_is_a_bound():
    assert_pair(
        bounded_by("CAST(s.updated_at AS BIGINT)", "CAST(l.label_ts AS BIGINT)"),
        bounded_by("CAST(s.updated_at AS BIGINT)", "CAST(l.label_ts AS BIGINT) + 86400"),
        **SPARK,
    )


def test_unix_timestamp_bounds_like_the_cast():
    assert_pair(
        bounded_by("unix_timestamp(s.updated_at)", "unix_timestamp(l.label_ts) - 3600"),
        bounded_by("unix_timestamp(s.updated_at)", "unix_timestamp(l.label_ts) + 3600"),
        **SPARK,
    )


def test_epoch_numbers_in_different_units_bound_nothing():
    # seconds compared with milliseconds: the condition holds for facts far in the future
    mixed = bounded_by("unix_seconds(s.updated_at)", "unix_millis(l.label_ts)")
    assert rules(mixed, **SPARK) != set()
    assert rules(bounded_by("unix_millis(s.updated_at)", "unix_seconds(l.label_ts)"), **SPARK)


def test_a_bare_constant_is_in_the_unit_of_the_epoch_function():
    # 86400 milliseconds is 86 seconds: still before the prediction time
    assert_pair(
        bounded_by("unix_millis(s.updated_at)", "unix_millis(l.label_ts) - 86400"),
        bounded_by("unix_millis(s.updated_at)", "unix_millis(l.label_ts) + 86400"),
        **SPARK,
    )


def test_epoch_ms_of_a_timestamp_column_in_duckdb():
    assert_pair(
        bounded_by("epoch_ms(s.updated_at)", "epoch_ms(l.label_ts) - 86400000"),
        bounded_by("epoch_ms(s.updated_at)", "epoch_ms(l.label_ts) + 86400000"),
    )


def test_a_cast_of_a_number_is_not_an_epoch():
    # user_stats.n_orders is a BIGINT: casting it to BIGINT gives a count, not a time
    query = f"""SELECT {KEYS}, SUM(s.total_spend) AS spend
FROM labels l LEFT JOIN user_stats s ON s.user_id = l.user_id
  AND CAST(s.n_orders AS BIGINT) < CAST(l.label_ts AS BIGINT)
GROUP BY 1, 2, 3"""
    assert rules(query, **SPARK) != set()


# --- months, quarters and years in a difference or a date_diff -----------------------------------


def stats_where(condition: str) -> str:
    return f"""SELECT {KEYS}, SUM(s.total_spend) AS spend
FROM labels l LEFT JOIN user_stats s ON s.user_id = l.user_id AND {condition}
GROUP BY 1, 2, 3"""


@pytest.mark.parametrize("unit", ["MONTH", "QUARTER", "YEAR"])
def test_a_calendar_interval_in_a_difference_is_a_bound(unit):
    assert_pair(
        stats_where(f"l.label_ts - s.updated_at > INTERVAL 1 {unit}"),
        stats_where(f"s.updated_at - l.label_ts > INTERVAL 1 {unit}"),
        "L1",
    )


@pytest.mark.parametrize("unit", ["month", "quarter", "year"])
def test_date_diff_in_calendar_units_is_a_bound(unit):
    assert_pair(
        stats_where(f"date_diff('{unit}', s.updated_at, l.label_ts) >= 1"),
        stats_where(f"date_diff('{unit}', s.updated_at, l.label_ts) >= 0"),
        "L1",
    )
    assert_pair(
        stats_where(f"date_diff('{unit}', s.updated_at, l.label_ts) > 0"),
        stats_where(f"date_diff('{unit}', l.label_ts, s.updated_at) > 0"),
    )


def test_date_diff_with_the_wrong_comparison_leaves_the_table_unbounded():
    # fewer than one month apart is a lower bound on the fact's time, not an upper one
    assert "L3" in rules(stats_where("date_diff('month', s.updated_at, l.label_ts) < 1"))


def events_where(condition: str) -> str:
    return f"""SELECT {KEYS}, SUM(e.amount) AS spend
FROM labels l LEFT JOIN events e ON e.user_id = l.user_id AND {condition}
GROUP BY 1, 2, 3"""


@pytest.mark.parametrize(
    "condition",
    [
        "l.label_ts - e.event_ts > INTERVAL 1 MONTH",
        "date_diff('month', e.event_ts, l.label_ts) > 1",
    ],
)
def test_a_month_counts_as_its_shortest_length_when_a_delay_is_declared(condition):
    # a month is 28 to 31 days: it covers a 20 day delay for sure, not a 30 day one
    assert rules(events_where(condition), max_ingest_delay={"events": "20 days"}) == set()
    assert "L4" in rules(events_where(condition), max_ingest_delay={"events": "30 days"})


def test_the_same_condition_in_days_covers_the_delay_it_names():
    exact = events_where("l.label_ts - e.event_ts > INTERVAL 31 DAY")
    assert rules(exact, max_ingest_delay={"events": "30 days"}) == set()
    assert "L4" in rules(
        events_where("l.label_ts - e.event_ts > INTERVAL 29 DAY"),
        max_ingest_delay={"events": "30 days"},
    )


# --- a self-join on an exact offset into the past -----------------------------------------------

from sqlguard.leakage.validator import validate  # noqa: E402


def verdict_and_rules(sql: str, spec: dict, schema: dict) -> tuple[str, set[str]]:
    report = validate(sql, LeakageSpec.from_dict(spec), schema=schema)
    return report.verdict, {f.rule_id for f in report.findings}


BARS_SPEC = {
    "label_table": "bars",
    "entity_keys": ["asset"],
    "prediction_ts": "ts",
    "label_columns": ["target"],
    "time_columns": {"bars": "ts"},
    "time_units": {"bars": "seconds"},
    "label_known_after": "15 minutes",
    "static_tables": [],
}
BARS_SCHEMA = {"bars": {"asset": "BIGINT", "ts": "BIGINT", "target": "DOUBLE", "close": "DOUBLE"}}
PRIOR_BAR = """SELECT t.ts, t.asset, t.target, p.{column} AS earlier
FROM bars AS t LEFT JOIN bars AS p ON p.asset = t.asset AND {condition}"""


def prior_bar(condition: str, column: str = "target") -> str:
    return PRIOR_BAR.format(condition=condition, column=column)


def test_a_bar_an_exact_offset_back_is_history_whose_label_is_final():
    clean = prior_bar("p.ts = t.ts - 960")
    assert verdict_and_rules(clean, BARS_SPEC, BARS_SCHEMA) == ("clean", set())


@pytest.mark.parametrize(
    "condition, rule",
    [
        ("p.ts = t.ts - 600", "L6"),  # the label of that bar is not final yet
        ("p.ts = t.ts + 960", "L2"),  # a bar in the future
        ("p.ts = t.ts - 960 + 1800", "L2"),
        ("p.ts >= t.ts - 960", "L2"),  # only a lower bound
    ],
)
def test_a_bar_at_another_offset_is_still_flagged(condition, rule):
    verdict, rules_found = verdict_and_rules(prior_bar(condition), BARS_SPEC, BARS_SCHEMA)
    assert verdict == "leaky" and rule in rules_found


def test_an_exact_offset_needs_the_unit_of_the_column():
    spec = {k: v for k, v in BARS_SPEC.items() if k != "time_units"}
    verdict, _ = verdict_and_rules(prior_bar("p.ts = t.ts - 960"), spec, BARS_SCHEMA)
    assert verdict == "leaky"  # 960 of what?


DAYS_SPEC = {
    "label_table": "sales",
    "entity_keys": ["store", "item"],
    "prediction_ts": "day",
    "label_columns": ["units"],
    "time_columns": {"sales": "day"},
    "static_tables": [],
}
DAYS_SCHEMA = {
    "sales": {"store": "BIGINT", "item": "BIGINT", "day": "DATE", "units": "DOUBLE", "promo": "INT"}
}
WEEK_AGO = """SELECT t.day, t.store, t.item, t.units, p.promo AS promo_then
FROM sales AS t LEFT JOIN sales AS p
  ON p.store = t.store AND p.item = t.item AND p.day = {day}"""


def test_a_day_an_exact_number_of_days_back_is_history():
    clean = WEEK_AGO.format(day="t.day - 7")
    assert verdict_and_rules(clean, DAYS_SPEC, DAYS_SCHEMA) == ("clean", set())
    interval = WEEK_AGO.format(day="t.day - INTERVAL 7 DAY")
    assert verdict_and_rules(interval, DAYS_SPEC, DAYS_SCHEMA) == ("clean", set())


@pytest.mark.parametrize("day", ["t.day + 7", "t.day - 7 + 14", "t.day + INTERVAL 7 DAY"])
def test_a_day_after_the_prediction_day_is_flagged(day):
    verdict, _ = verdict_and_rules(WEEK_AGO.format(day=day), DAYS_SPEC, DAYS_SCHEMA)
    assert verdict == "leaky"


def test_a_bare_number_means_days_only_for_a_date_column():
    schema = {"sales": {**DAYS_SCHEMA["sales"], "day": "BIGINT"}}
    verdict, _ = verdict_and_rules(WEEK_AGO.format(day="t.day - 7"), DAYS_SPEC, schema)
    assert verdict == "leaky"


def test_a_daily_snapshot_joined_on_the_previous_day_is_a_bound():
    snapshot = f"""SELECT {KEYS}, SUM(s.total_spend) AS spend
FROM labels l LEFT JOIN user_stats s ON s.user_id = l.user_id
  AND s.updated_at = l.label_ts - INTERVAL 1 DAY
GROUP BY 1, 2, 3"""
    same_instant = snapshot.replace(" - INTERVAL 1 DAY", "")
    next_day = snapshot.replace("- INTERVAL 1 DAY", "+ INTERVAL 1 DAY")
    assert rules(snapshot) == set()
    assert rules(next_day) != set()
    assert rules(same_instant) != set()  # a fact stamped at the prediction time is not yet known
