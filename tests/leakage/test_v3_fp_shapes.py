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


# --- the label filter of a staging step when the output does not carry the prediction time ------


def staged(filter_sql: str, tail: str = "SELECT user_id, churned FROM cleaned") -> str:
    return f"""WITH source AS (SELECT * FROM labels),
cleaned AS (SELECT * FROM source WHERE {filter_sql})
{tail}"""


def test_a_label_filter_in_a_staging_step_is_not_a_use_when_the_spec_error_is_reported():
    # the output has no prediction time, which is reported as a spec error; the filter still only
    # chooses which rows are the output rows, as it does when the prediction time is there
    found = rules(staged("user_id IS NOT NULL AND churned IS NOT NULL"))
    assert found == {"SPEC"}
    with_time = staged(
        "user_id IS NOT NULL AND churned IS NOT NULL",
        "SELECT user_id, label_ts, churned FROM cleaned",
    )
    assert rules(with_time) == set()


def staged_after_a_join(filter_sql: str) -> str:
    return f"""WITH source AS (SELECT * FROM labels),
joined AS (SELECT s.user_id, s.churned, e.amount FROM source s LEFT JOIN events e
           ON e.user_id = s.user_id),
cleaned AS (SELECT * FROM joined WHERE {filter_sql})
SELECT user_id, churned, amount FROM cleaned"""


@pytest.mark.parametrize(
    "query",
    [
        # the filter mixes the label with a feature
        staged_after_a_join("churned IS NOT NULL OR amount > 0"),
        # the filtered rows feed a feature (they are joined back, or read twice), not the output
        """WITH source AS (SELECT * FROM labels),
cleaned AS (SELECT * FROM source WHERE churned = 1)
SELECT s.user_id, s.churned, c.user_id IS NOT NULL AS ever_churned
FROM source s LEFT JOIN cleaned c ON c.user_id = s.user_id""",
        """WITH source AS (SELECT * FROM labels),
cleaned AS (SELECT * FROM source WHERE churned = 1)
SELECT s.user_id, s.churned,
       (SELECT COUNT(*) FROM cleaned c WHERE c.user_id = s.user_id) AS n
FROM source s""",
        # rows were grouped or ranked before the filter
        """WITH source AS (SELECT * FROM labels),
grouped AS (SELECT user_id, MAX(churned) AS m FROM source GROUP BY user_id)
SELECT user_id, m FROM grouped""",
        """WITH source AS (SELECT * FROM labels),
ranked AS (SELECT *, ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY churned) AS rn FROM source),
cleaned AS (SELECT * FROM ranked WHERE rn = 1 AND churned IS NOT NULL)
SELECT user_id, churned FROM cleaned""",
        # the label chooses rows through a subquery, which is not an output row scope
        """WITH source AS (SELECT * FROM labels), cleaned AS (SELECT * FROM source)
SELECT user_id, churned FROM labels l
WHERE user_id IN (SELECT user_id FROM cleaned WHERE churned = 1)""",
        # the label feeds a column
        staged("churned IS NOT NULL", "SELECT user_id, churned * 2 AS doubled FROM cleaned"),
    ],
)
def test_a_label_filter_that_is_not_a_plain_filter_of_the_output_rows_is_still_a_use(query):
    assert "L6" in rules(query)


# --- a column fed by a UNION or an EXCEPT that is joined to the spine ----------------------------


def facts_union(
    operator: str = "UNION",
    first_bound: str = "e.ingested_at < l.label_ts",
    second_bound: str = "s.updated_at < l.label_ts",
) -> str:
    return f"""WITH active AS (
  SELECT l.user_id, l.label_ts, e.event_id AS ref
  FROM labels l JOIN events e ON e.user_id = l.user_id AND {first_bound}
  {operator}
  SELECT l.user_id, l.label_ts, s.n_orders AS ref
  FROM labels l JOIN user_stats s ON s.user_id = l.user_id AND {second_bound}
)
SELECT {KEYS}, COUNT(a.ref) AS n
FROM labels l LEFT JOIN active a ON a.user_id = l.user_id AND a.label_ts = l.label_ts
GROUP BY 1, 2, 3"""


@pytest.mark.parametrize("operator", ["UNION", "UNION ALL", "INTERSECT"])
def test_facts_gathered_by_a_set_operation_are_bounded_in_each_branch(operator):
    assert rules(facts_union(operator)) == set()


@pytest.mark.parametrize(
    "first, second",
    [
        ("e.ingested_at < l.label_ts + INTERVAL 1 DAY", "s.updated_at < l.label_ts"),
        ("e.ingested_at < l.label_ts", "s.updated_at < l.label_ts + INTERVAL 1 DAY"),
        ("e.ingested_at < l.label_ts", "s.updated_at >= l.label_ts - INTERVAL 7 DAY"),
        ("e.ingested_at <= l.label_ts", "s.updated_at < l.label_ts"),
        ("e.user_id > 0", "s.updated_at < l.label_ts"),
    ],
)
def test_a_branch_with_a_wrong_or_missing_bound_is_still_flagged(first, second):
    for operator in ("UNION", "UNION ALL"):
        assert rules(facts_union(operator, first, second)), (operator, first, second)


def test_a_branch_that_is_not_a_prediction_time_keeps_the_union_off_the_spine():
    shifted = facts_union().replace(
        "SELECT l.user_id, l.label_ts, s.n_orders AS ref",
        "SELECT l.user_id, l.label_ts + INTERVAL 1 DAY AS label_ts, s.n_orders AS ref",
    )
    assert rules(shifted)
    other = facts_union().replace(
        "SELECT l.user_id, l.label_ts, s.n_orders AS ref\n  FROM labels l JOIN user_stats s",
        "SELECT s.user_id, s.updated_at AS label_ts, s.n_orders AS ref\n  FROM user_stats s "
        "JOIN labels l",
    )
    assert rules(other)


def test_a_union_joined_without_its_prediction_time_is_not_on_the_spine():
    loose = facts_union().replace(" AND a.label_ts = l.label_ts", "")
    assert rules(loose)


def lapsed(recent_source: str = "FROM known") -> str:
    return f"""WITH known AS (
  SELECT l.user_id, l.label_ts, e.event_ts FROM labels l
  JOIN events e ON e.user_id = l.user_id AND e.ingested_at < l.label_ts
),
earlier AS (SELECT user_id, label_ts FROM known WHERE event_ts < label_ts - INTERVAL 21 DAY),
recent AS (SELECT user_id, label_ts {recent_source} WHERE event_ts >= label_ts - INTERVAL 21 DAY),
gone AS (SELECT user_id, label_ts FROM earlier EXCEPT SELECT user_id, label_ts FROM recent)
SELECT {KEYS}, g.user_id IS NOT NULL AS lapsed
FROM labels l LEFT JOIN gone g ON g.user_id = l.user_id AND g.label_ts = l.label_ts"""


def test_rows_removed_by_an_except_over_bounded_facts_are_clean():
    assert rules(lapsed()) == set()


def test_an_except_whose_removed_side_reads_unbounded_facts_is_flagged():
    future = lapsed(
        "FROM (SELECT l.user_id, l.label_ts, e.event_ts FROM labels l "
        "JOIN events e ON e.user_id = l.user_id) k"
    )
    assert rules(future)


# --- a timeline of facts and spine rows read through a named window -----------------------------


def timeline(
    frame: str = "ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING",
    kinds: tuple[int, int] = (1, 0),
    fact_time: str = "ingested_at",
    extra: str = "",
    named: bool = True,
) -> str:
    over, definition = (
        ("w", "WINDOW w AS (PARTITION BY user_id ORDER BY ts, kind {frame})")
        if (named)
        else ("(PARTITION BY user_id ORDER BY ts, kind {frame})", "")
    )
    return f"""WITH timeline AS (
  SELECT user_id, {fact_time} AS ts, {kinds[0]} AS kind, amount,
         CAST(NULL AS TIMESTAMP) AS label_ts, CAST(NULL AS BIGINT) AS churned
  FROM events
  UNION ALL
  SELECT user_id, label_ts AS ts, {kinds[1]} AS kind, CAST(NULL AS DOUBLE) AS amount,
         label_ts, churned
  FROM labels
),
scored AS (
  SELECT *, SUM(amount) OVER {over.format(frame=frame)} AS spend,
         COUNT(amount) OVER {over.format(frame=frame)} AS n{extra}
  FROM timeline
  {definition.format(frame=frame)}
)
SELECT user_id, label_ts, churned, spend, n FROM scored WHERE kind = {kinds[1]}"""


def test_a_named_window_over_a_timeline_ends_before_the_spine_row():
    assert rules(timeline()) == set()
    assert rules(timeline(named=False)) == set()


@pytest.mark.parametrize(
    "variant",
    [
        {"frame": "ROWS BETWEEN UNBOUNDED PRECEDING AND 1 FOLLOWING"},
        {"frame": "ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING"},
        {"kinds": (0, 1)},  # facts sort ahead of the spine row they tie with
        {"fact_time": "event_ts"},  # ordered by event time, not by when the fact was known
        {"extra": ", SUM(amount) OVER (PARTITION BY user_id) AS lifetime"},
    ],
)
def test_the_same_named_window_with_one_thing_wrong_is_still_flagged(variant):
    for named in (True, False):
        assert rules(timeline(named=named, **variant)), (variant, named)


# --- labels that are final by the time a window or a guard reads them -----------------------------

FINAL = {"label_known_after": "14 days"}


def cohort_rate(frame: str, order: str = "label_ts", source: str | None = None) -> str:
    source = source or (
        "SELECT l.user_id, l.label_ts, l.churned, en.country "
        "FROM labels l LEFT JOIN entities en ON en.user_id = l.user_id"
    )
    return f"""WITH spine AS ({source})
SELECT user_id, label_ts, churned, country, AVG(churned) OVER w AS cohort_rate
FROM spine
WINDOW w AS (PARTITION BY country ORDER BY {order} {frame})"""


def test_the_final_labels_of_a_cohort_in_a_range_frame_that_stops_long_enough_before():
    assert_pair(
        cohort_rate("RANGE BETWEEN UNBOUNDED PRECEDING AND INTERVAL 15 DAY PRECEDING"),
        cohort_rate("RANGE BETWEEN UNBOUNDED PRECEDING AND INTERVAL 10 DAY PRECEDING"),
        "L6",
        **FINAL,
    )


@pytest.mark.parametrize(
    "frame, order",
    [
        ("RANGE BETWEEN UNBOUNDED PRECEDING AND INTERVAL 14 DAY PRECEDING", "label_ts"),
        ("RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW", "label_ts"),
        ("RANGE BETWEEN UNBOUNDED PRECEDING AND INTERVAL 15 DAY FOLLOWING", "label_ts"),
        ("ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING", "label_ts"),
        ("RANGE BETWEEN UNBOUNDED PRECEDING AND INTERVAL 15 DAY PRECEDING", "label_ts DESC"),
        ("RANGE BETWEEN UNBOUNDED PRECEDING AND INTERVAL 15 DAY PRECEDING", "user_id"),
    ],
)
def test_a_cohort_frame_that_does_not_stop_in_time_still_reads_the_outcome(frame, order):
    assert "L6" in rules(cohort_rate(frame, order), **FINAL), (frame, order)


def test_a_cohort_over_rows_joined_to_a_table_with_history_is_not_accepted():
    source = (
        "SELECT l.user_id, l.label_ts, l.churned, e.user_id AS country "
        "FROM labels l LEFT JOIN events e ON e.user_id = l.user_id AND e.ingested_at < l.label_ts"
    )
    frame = "RANGE BETWEEN UNBOUNDED PRECEDING AND INTERVAL 15 DAY PRECEDING"
    assert "L6" in rules(cohort_rate(frame, source=source), **FINAL)


def previous_label(guard: str, then: str = "LAG(churned) OVER w", otherwise: str = "") -> str:
    return f"""SELECT user_id, label_ts, churned,
  CASE WHEN {guard} THEN {then} {otherwise} END AS previous_label
FROM labels
WINDOW w AS (PARTITION BY user_id ORDER BY label_ts)"""


def test_the_previous_label_is_history_when_the_guard_says_it_was_final():
    assert_pair(
        previous_label("LAG(label_ts) OVER w + INTERVAL 14 DAY < label_ts"),
        previous_label("LAG(label_ts) OVER w + INTERVAL 7 DAY < label_ts"),
        "L6",
        **FINAL,
    )


@pytest.mark.parametrize(
    "guard",
    [
        "LAG(label_ts) OVER w < label_ts - INTERVAL 15 DAY",
        "label_ts - LAG(label_ts) OVER w > INTERVAL 14 DAY",
        "label_ts > LAG(label_ts) OVER w + INTERVAL 14 DAY",
        "LAG(label_ts) OVER w + INTERVAL 14 DAY <= label_ts - INTERVAL 1 SECOND",
        "LAG(label_ts) OVER w IS NOT NULL AND LAG(label_ts) OVER w + INTERVAL 20 DAY < label_ts",
    ],
)
def test_other_spellings_of_the_same_guard(guard):
    assert "L6" not in rules(previous_label(guard), **FINAL), guard


@pytest.mark.parametrize(
    "guard, then, otherwise",
    [
        ("LAG(label_ts) OVER w + INTERVAL 14 DAY <= label_ts", "LAG(churned) OVER w", ""),
        ("LAG(label_ts) OVER w + INTERVAL 14 DAY > label_ts", "LAG(churned) OVER w", ""),
        ("LAG(label_ts) OVER w + INTERVAL 14 DAY < label_ts", "0", "ELSE LAG(churned) OVER w"),
        ("label_ts - LAG(label_ts) OVER w < INTERVAL 20 DAY", "LAG(churned) OVER w", ""),
        ("LAG(label_ts, 1) OVER w + INTERVAL 14 DAY < label_ts", "LAG(churned, 2) OVER w", ""),
        (
            "LAG(label_ts) OVER w + INTERVAL 14 DAY < label_ts OR user_id > 0",
            "LAG(churned) OVER w",
            "",
        ),
        ("label_ts + INTERVAL 14 DAY < LAG(label_ts) OVER w", "LAG(churned) OVER w", ""),
        ("LEAD(label_ts) OVER w + INTERVAL 14 DAY < label_ts", "LEAD(churned) OVER w", ""),
    ],
)
def test_a_guard_that_does_not_make_the_previous_label_final_is_still_flagged(
    guard, then, otherwise
):
    assert "L6" in rules(previous_label(guard, then, otherwise), **FINAL), guard


def test_the_guard_must_read_the_same_window_as_the_label():
    query = """SELECT user_id, label_ts, churned,
  CASE WHEN LAG(label_ts) OVER (PARTITION BY user_id ORDER BY label_ts) + INTERVAL 14 DAY < label_ts
       THEN LAG(churned) OVER (ORDER BY label_ts) END AS previous_label
FROM labels"""
    assert "L6" in rules(query, **FINAL)


# --- the validity interval of a version computed inside the filter that uses it ------------------


def as_of(
    qualify: str,
    extra: str = "",
    partition: str = "s.user_id, l.label_ts",
    arg: str = "s.updated_at",
    join_bound: str = "",
) -> str:
    lead = f"LEAD({arg}) OVER (PARTITION BY {partition} ORDER BY s.updated_at)"
    return f"""SELECT {KEYS}, s.total_spend{extra}
FROM labels l JOIN user_stats s ON s.user_id = l.user_id{join_bound}
QUALIFY {qualify.format(lead=lead)}"""


VALID_AT = "l.label_ts > s.updated_at AND l.label_ts <= COALESCE({lead}, TIMESTAMP '9999-12-31')"


def test_the_version_valid_at_the_prediction_time_chosen_in_qualify():
    clean = as_of(VALID_AT)
    assert rules(clean) == set()
    assert rules(
        as_of("l.label_ts > s.updated_at AND ({lead} IS NULL OR {lead} >= l.label_ts)")
    ) == (set())
    # the bound may also be written in the join
    assert (
        rules(
            as_of(
                "({lead} IS NULL OR {lead} >= l.label_ts)",
                join_bound=" AND s.updated_at < l.label_ts",
            )
        )
        == set()
    )


@pytest.mark.parametrize(
    "variant",
    [
        # the window mixes the rows of several predictions
        {"partition": "s.user_id"},
        # the next version is not the next time of the version rows
        {"arg": "s.total_spend"},
        # the next version reaches the output
        {
            "extra": ", LEAD(s.updated_at) OVER (PARTITION BY s.user_id, l.label_ts "
            "ORDER BY s.updated_at) AS next_version"
        },
        # tested against something other than the prediction time
        {"qualify": "l.label_ts > s.updated_at AND {lead} > l.label_ts + INTERVAL 30 DAY"},
        {"qualify": "l.label_ts > s.updated_at AND {lead} > s.updated_at"},
        # the version is not bounded before the prediction time at all
        {"qualify": "{lead} IS NULL OR {lead} >= l.label_ts"},
        {"qualify": "l.label_ts >= s.updated_at AND ({lead} IS NULL OR {lead} >= l.label_ts)"},
    ],
)
def test_the_same_filter_with_one_thing_wrong_is_still_flagged(variant):
    assert rules(as_of(**{"qualify": VALID_AT, **variant})), variant


# --- an approximate distinct count beside the filtered aggregates ---------------------------------


def filtered_sum(other: str, *, filtered: bool = True) -> str:
    bound = "FILTER (WHERE e.ingested_at < l.label_ts)" if filtered else ""
    return f"""SELECT {KEYS}, SUM(e.amount) {bound} AS spend, {other} AS other
FROM labels l LEFT JOIN events e ON e.user_id = l.user_id
GROUP BY 1, 2, 3"""


@pytest.mark.parametrize("name", ["approx_count_distinct", "approx_distinct"])
def test_an_approximate_distinct_count_of_the_spine_ignores_how_many_rows_were_joined(name):
    assert_pair(
        filtered_sum(f"{name}(l.user_id)"),
        filtered_sum(f"{name}(l.user_id)", filtered=False),
        "L3",
    )


@pytest.mark.parametrize("name", ["approx_count_distinct", "approx_distinct"])
def test_an_approximate_distinct_count_of_the_joined_table_is_a_use_of_all_its_rows(name):
    assert "L3" in rules(filtered_sum(f"{name}(e.user_id)"))


# --- a time-split cutoff computed from prediction times -------------------------------------------

SPLIT_VALUE = "MAX(label_ts) - INTERVAL 30 DAY"


def split_where(cutoff: str, op: str = "<") -> str:
    return f"SELECT {KEYS} FROM labels l WHERE l.label_ts {op} {cutoff}"


def split_with(value: str, use: str, op: str = "<") -> str:
    return f"""WITH cutoff AS (SELECT {value} AS c FROM labels)
SELECT {KEYS} FROM labels l CROSS JOIN cutoff WHERE l.label_ts {op} {use}"""


def test_a_cutoff_with_arithmetic_on_the_aggregate_picks_training_rows():
    assert_pair(
        split_where(f"(SELECT {SPLIT_VALUE} FROM labels)"),
        split_where(f"(SELECT {SPLIT_VALUE} FROM labels)", ">="),
        "L3",
    )
    assert rules(split_where("(SELECT MIN(label_ts) + INTERVAL 1 YEAR FROM labels)")) == set()
    assert rules(split_where("(SELECT quantile_cont(label_ts, 0.8) FROM labels)")) == set()


def test_a_cutoff_held_in_a_cte_or_a_derived_table_picks_training_rows():
    assert_pair(
        split_with(SPLIT_VALUE, "cutoff.c"),
        split_with(SPLIT_VALUE, "cutoff.c", ">="),
        "L3",
    )
    assert rules(split_with("MAX(label_ts)", "(SELECT c FROM cutoff)")) == set()
    derived = f"""SELECT {KEYS} FROM labels l,
  (SELECT {SPLIT_VALUE} AS c FROM labels) k WHERE l.label_ts < k.c"""
    assert rules(derived) == set()
    assert "L3" in rules(derived.replace("< k.c", ">= k.c"))


@pytest.mark.parametrize(
    "query",
    [
        # the cutoff reaches a feature, not a row filter
        f"SELECT {KEYS}, (SELECT {SPLIT_VALUE} FROM labels) AS cutoff_ts FROM labels l",
        f"""WITH cutoff AS (SELECT {SPLIT_VALUE} AS c FROM labels)
SELECT {KEYS}, cutoff.c AS cutoff_ts FROM labels l CROSS JOIN cutoff""",
        # the cutoff depends on more than prediction times
        f"SELECT {KEYS} FROM labels l WHERE l.label_ts < "
        "(SELECT MAX(label_ts) FROM labels WHERE churned = 1)",
        f"SELECT {KEYS} FROM labels l WHERE l.label_ts < "
        "(SELECT MAX(label_ts) + INTERVAL 1 DAY * churned FROM labels)",
        # one cutoff per entity is a feature of the entity's own history
        f"""WITH cutoff AS (SELECT user_id, MAX(label_ts) AS c FROM labels GROUP BY user_id)
SELECT {KEYS} FROM labels l JOIN cutoff ON cutoff.user_id = l.user_id
WHERE l.label_ts < cutoff.c""",
        # compared with something that is not a prediction time
        f"""WITH cutoff AS (SELECT {SPLIT_VALUE} AS c FROM labels)
SELECT {KEYS} FROM labels l CROSS JOIN cutoff WHERE l.churned < cutoff.c""",
        # the cutoff is read both in the filter and as a feature
        f"""WITH cutoff AS (SELECT {SPLIT_VALUE} AS c FROM labels)
SELECT {KEYS}, cutoff.c AS again FROM labels l CROSS JOIN cutoff WHERE l.label_ts < cutoff.c""",
    ],
)
def test_a_cutoff_used_any_other_way_is_still_flagged(query):
    assert rules(query), query
