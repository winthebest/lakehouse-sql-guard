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
