"""Time semantics of the static analysis: a label table that is also event history, a single
series (no entity key), labels that are final the moment they are stamped, integer time columns
with a declared unit, and windows that read only the labels of earlier rows.

Every accepted shape is paired with a query that differs in one condition and must stay flagged.
"""

import pytest

from sqlguard.leakage.spec import LeakageSpec
from sqlguard.leakage.validator import validate


def check(sql: str, spec: dict, schema: dict) -> tuple[str, set[str]]:
    report = validate(sql, LeakageSpec.from_dict(spec), schema=schema)
    return report.verdict, {f.rule_id for f in report.findings}


# --- a label table that is also event history -----------------------------------------------

LOG_SPEC = {
    "label_table": "visits",
    "entity_keys": ["visitor_id"],
    "prediction_ts": "scored_at",
    "label_columns": ["converted"],
    "time_columns": {"visits": "visit_ts"},
    "availability_columns": {"visits": "loaded_at"},
    "static_tables": [],
}
LOG_SCHEMA = {
    "visits": {
        "visitor_id": "BIGINT",
        "scored_at": "TIMESTAMP",
        "visit_ts": "TIMESTAMP",
        "loaded_at": "TIMESTAMP",
        "pages": "BIGINT",
        "converted": "BIGINT",
    }
}
PRIOR = """SELECT l.visitor_id, l.scored_at, l.converted,
  (SELECT COUNT(*) FROM visits h WHERE h.visitor_id = l.visitor_id{where}) AS prior_visits
FROM visits l"""


def prior(where: str) -> str:
    return PRIOR.format(where=f" AND {where}" if where else "")


HISTORY_CLEAN = {
    "event time and load time both before the prediction": (
        "h.visit_ts < l.scored_at AND h.loaded_at < l.scored_at"
    ),
    "load time alone before the prediction": "h.loaded_at < l.scored_at",
}
HISTORY_LEAKY = {
    "no bound at all": ("", "L3"),
    "bound on a column the spec does not give as the history time": (
        "h.scored_at < l.scored_at",
        "L3",
    ),
    "bound reaches past the prediction": ("h.visit_ts < l.scored_at + INTERVAL 2 DAY", "L1"),
    "inclusive bound on load time": ("h.loaded_at <= l.scored_at", "L5"),
}


@pytest.mark.parametrize("name", HISTORY_CLEAN)
def test_history_of_the_label_table_is_bounded_by_its_declared_time(name):
    assert check(prior(HISTORY_CLEAN[name]), LOG_SPEC, LOG_SCHEMA) == ("clean", set())


@pytest.mark.parametrize("name", HISTORY_LEAKY)
def test_history_of_the_label_table_without_a_proper_bound_is_flagged(name):
    where, rule = HISTORY_LEAKY[name]
    verdict, rules = check(prior(where), LOG_SPEC, LOG_SCHEMA)
    assert verdict == "leaky"
    assert rule in rules


def test_event_time_bound_alone_leaves_the_late_arrival_hint_of_a_temporal_table():
    verdict, rules = check(prior("h.visit_ts < l.scored_at"), LOG_SPEC, LOG_SCHEMA)
    assert rules == {"L4"}
    assert verdict == "unverifiable"


def test_label_table_time_column_defaults_to_the_prediction_time_when_not_declared():
    spec = {k: v for k, v in LOG_SPEC.items() if k not in ("time_columns", "availability_columns")}
    spec["time_columns"] = {"other": "x"}
    clean = prior("h.scored_at < l.scored_at")
    verdict, rules = check(clean, spec, {**LOG_SCHEMA, "other": {"x": "TIMESTAMP"}})
    assert (verdict, rules) == ("clean", set())


def test_declared_time_columns_of_the_label_table_must_exist_in_the_schema():
    schema = {"visits": {k: v for k, v in LOG_SCHEMA["visits"].items() if k != "loaded_at"}}
    report = validate(
        prior("h.visit_ts < l.scored_at"), LeakageSpec.from_dict(LOG_SPEC), schema=schema
    )
    assert report.verdict == "unverifiable"
    assert [f.message for f in report.findings if f.rule_id == "SPEC"] == [
        "spec column visits.loaded_at not in schema"
    ]


def test_a_derived_label_declared_in_output_roles_need_not_be_a_schema_column():
    spec = {
        **LOG_SPEC,
        "label_columns": [],
        "output_roles": {"label": ["will_convert"]},
    }
    report = validate(
        prior("h.visit_ts < l.scored_at AND h.loaded_at < l.scored_at"),
        LeakageSpec.from_dict(spec),
        schema=LOG_SCHEMA,
    )
    assert not [f for f in report.findings if f.rule_id == "SPEC"]


# --- labels that are final the moment their row is stamped ------------------------------------

PLAYS_SPEC = {
    "label_table": "plays",
    "entity_keys": ["user_id"],
    "prediction_ts": "ts",
    "label_columns": ["correct"],
    "time_columns": {"plays": "ts"},
    "static_tables": [],
}
PLAYS_SCHEMA = {
    "plays": {
        "user_id": "BIGINT",
        "ts": "TIMESTAMP",
        "task": "BIGINT",
        "correct": "BIGINT",
    }
}
PRIOR_RATE = """SELECT l.user_id, l.ts, l.correct,
  (SELECT AVG(h.correct) FROM plays h WHERE h.user_id = l.user_id AND {bound}) AS prior_rate
FROM plays l"""


def with_delay(value, spec=PLAYS_SPEC):
    return {**spec, "label_known_after": value}


def test_labels_of_strictly_earlier_rows_are_known_when_the_delay_is_zero():
    sql = PRIOR_RATE.format(bound="h.ts < l.ts")
    assert check(sql, with_delay("0 seconds"), PLAYS_SCHEMA) == ("clean", set())
    assert check(sql, with_delay(0), PLAYS_SCHEMA) == ("clean", set())


def test_zero_delay_written_as_a_number_equals_the_zero_duration():
    sql = PRIOR_RATE.format(bound="h.ts < l.ts")
    assert check(sql, with_delay(0), PLAYS_SCHEMA) == check(
        sql, with_delay("0 seconds"), PLAYS_SCHEMA
    )


def test_no_declared_delay_leaves_the_earlier_labels_a_use_of_the_target():
    sql = PRIOR_RATE.format(bound="h.ts < l.ts")
    assert check(sql, PLAYS_SPEC, PLAYS_SCHEMA) == ("leaky", {"L6"})


def test_a_row_stamped_at_the_prediction_time_has_no_final_label_yet():
    sql = PRIOR_RATE.format(bound="h.ts <= l.ts")
    verdict, rules = check(sql, with_delay(0), PLAYS_SCHEMA)
    assert verdict == "leaky"
    assert {"L5", "L6"} <= rules


def test_a_positive_delay_keeps_recent_labels_unknown():
    strict = PRIOR_RATE.format(bound="h.ts < l.ts")
    assert check(strict, with_delay("1 hour"), PLAYS_SCHEMA)[1] == {"L6"}
    old_enough = PRIOR_RATE.format(bound="h.ts < l.ts - INTERVAL 2 HOUR")
    assert check(old_enough, with_delay("1 hour"), PLAYS_SCHEMA) == ("clean", set())
    too_recent = PRIOR_RATE.format(bound="h.ts < l.ts - INTERVAL 30 MINUTE")
    assert check(too_recent, with_delay("1 hour"), PLAYS_SCHEMA)[1] == {"L6"}


# --- a single series: no entity key --------------------------------------------------------

SERIES_SPEC = {
    "label_table": "bars",
    "entity_keys": [],
    "prediction_ts": "ts",
    "label_columns": ["target"],
    "time_columns": {"bars": "ts", "ticks": "tick_ts"},
    "static_tables": [],
}
SERIES_SCHEMA = {
    "bars": {"ts": "TIMESTAMP", "close": "DOUBLE", "target": "DOUBLE"},
    "ticks": {"tick_ts": "TIMESTAMP", "price": "DOUBLE"},
}
SERIES_CLEAN = {
    "trailing average that stops before the current row": (
        "SELECT ts, target, AVG(close) OVER (ORDER BY ts "
        "ROWS BETWEEN 5 PRECEDING AND 1 PRECEDING) AS ma FROM bars"
    ),
    "previous close": "SELECT ts, target, LAG(close, 1) OVER (ORDER BY ts) AS prev FROM bars",
    "ticks before the bar": (
        "SELECT b.ts, b.target, (SELECT SUM(t.price) FROM ticks t WHERE t.tick_ts < b.ts) AS vol "
        "FROM bars b"
    ),
    "ticks joined before the bar": (
        "SELECT b.ts, b.target, COUNT(t.price) AS n FROM bars b "
        "LEFT JOIN ticks t ON t.tick_ts < b.ts GROUP BY b.ts, b.target"
    ),
}
SERIES_LEAKY = {
    "next close": ("SELECT ts, target, LEAD(close) OVER (ORDER BY ts) AS nxt FROM bars", "L1"),
    "average over the whole series": (
        "SELECT ts, target, AVG(close) OVER () AS m FROM bars",
        "L3",
    ),
    "ticks with no bound": (
        "SELECT b.ts, b.target, (SELECT SUM(t.price) FROM ticks t) AS vol FROM bars b",
        "L3",
    ),
    "ticks up to and including the bar": (
        "SELECT b.ts, b.target, COUNT(t.price) AS n FROM bars b "
        "LEFT JOIN ticks t ON t.tick_ts <= b.ts GROUP BY b.ts, b.target",
        "L5",
    ),
}


@pytest.mark.parametrize("name", SERIES_CLEAN)
def test_a_series_without_entity_keys_passes_when_every_read_stays_before_the_row(name):
    assert check(SERIES_CLEAN[name], SERIES_SPEC, SERIES_SCHEMA) == ("clean", set())


@pytest.mark.parametrize("name", SERIES_LEAKY)
def test_a_series_without_entity_keys_is_still_checked(name):
    sql, rule = SERIES_LEAKY[name]
    verdict, rules = check(sql, SERIES_SPEC, SERIES_SCHEMA)
    assert verdict == "leaky" and rule in rules


KEYED_SERIES = """WITH sp AS (SELECT ts, target, {key} AS sk FROM bars),
pit AS (
  SELECT sp.sk, t.price FROM sp JOIN ticks t ON t.tick_ts < sp.ts
  QUALIFY row_number() OVER (PARTITION BY {part} ORDER BY t.tick_ts DESC) = 1)
SELECT sp.ts, sp.target, pit.price FROM sp LEFT JOIN pit ON pit.sk = sp.sk"""


def test_the_prediction_time_alone_is_the_row_key_of_a_series():
    sql = KEYED_SERIES.format(key="md5(CAST(ts AS VARCHAR))", part="sp.sk")
    assert check(sql, SERIES_SPEC, SERIES_SCHEMA) == ("clean", set())


def test_a_key_that_only_names_the_day_is_not_the_row_key_of_a_series():
    sql = KEYED_SERIES.format(key="md5(CAST(CAST(ts AS DATE) AS VARCHAR))", part="sp.sk")
    assert check(sql, SERIES_SPEC, SERIES_SCHEMA)[0] == "leaky"


FIX_SPEC = {**SERIES_SPEC, "availability_columns": {"ticks": "loaded_at"}}
FIX_SCHEMA = {
    **SERIES_SCHEMA,
    "ticks": {"tick_ts": "TIMESTAMP", "loaded_at": "TIMESTAMP", "price": "DOUBLE"},
}
SERIES_FIXES = {
    "aggregate with no bound": (
        "SELECT b.ts, b.target, (SELECT SUM(t.price) FROM ticks t) AS vol FROM bars b",
        ["WHERE t.tick_ts < b.ts AND t.loaded_at < b.ts"],
    ),
    "latest tick is ranked per prediction time alone": (
        "SELECT b.ts, b.target, t.price FROM bars b LEFT JOIN (SELECT * FROM ticks "
        "QUALIFY row_number() OVER (ORDER BY tick_ts DESC) = 1) t ON TRUE",
        [
            "PARTITION BY b.ts ORDER BY t.tick_ts DESC, t.loaded_at DESC",
            "rows are ranked per (ts) of the spine",
        ],
    ),
}


@pytest.mark.parametrize("name", SERIES_FIXES)
def test_the_fix_for_a_series_ranks_and_bounds_without_an_entity_key(name):
    import re

    from sqlguard.leakage.fixes import Verification, suggest_fix
    from sqlguard.leakage.validator import static_findings

    sql, expected = SERIES_FIXES[name]
    spec = LeakageSpec.from_dict(FIX_SPEC)
    fix = suggest_fix(sql, spec, schema=FIX_SCHEMA)[-1]
    assert fix.after_sql is not None and fix.verification is Verification.STATIC_ONLY
    assert static_findings(fix.after_sql, spec, FIX_SCHEMA) == []
    text = " ".join([re.sub(r"\s+", " ", fix.after_sql), *fix.caveats])
    for snippet in expected:
        assert snippet in text, (snippet, text)


# --- integer time columns with a declared unit -----------------------------------------------

MS_SPEC = {
    "label_table": "train",
    "entity_keys": ["user_id"],
    "prediction_ts": "ts",
    "label_columns": ["ans"],
    "time_columns": {"train": "ts", "events": "event_ts"},
    "time_units": {"train": "milliseconds", "events": "milliseconds"},
    "static_tables": [],
}
MS_SCHEMA = {
    "train": {"user_id": "BIGINT", "ts": "BIGINT", "ans": "BIGINT"},
    "events": {"user_id": "BIGINT", "event_ts": "BIGINT", "amount": "DOUBLE"},
}
EVENTS_SUM = """SELECT l.user_id, l.ts, l.ans,
  (SELECT SUM(e.amount) FROM events e WHERE e.user_id = l.user_id AND {bound}) AS spend
FROM train l"""
DAY_MS = 86_400_000

INT_CLEAN = {
    "a day back in milliseconds": f"e.event_ts < l.ts - {DAY_MS}",
    "the day written as a product": "e.event_ts < l.ts - 24 * 60 * 60 * 1000",
    "plain strict bound": "e.event_ts < l.ts",
    "difference of the two columns": f"l.ts - e.event_ts > {DAY_MS}",
    "difference compared the other way round": f"{DAY_MS} < l.ts - e.event_ts",
}
INT_LEAKY = {
    "a second ahead": ("e.event_ts <= l.ts + 1000", "L5"),
    "a day ahead": (f"e.event_ts < l.ts + {DAY_MS}", "L1"),
    "inclusive": ("e.event_ts <= l.ts", "L5"),
    "difference reaches past the prediction": (f"l.ts - e.event_ts > -{DAY_MS}", "L1"),
    "no bound": ("e.amount > 0", "L3"),
}


@pytest.mark.parametrize("name", INT_CLEAN)
def test_a_constant_added_to_an_integer_time_is_read_in_the_declared_unit(name):
    sql = EVENTS_SUM.format(bound=INT_CLEAN[name])
    assert check(sql, MS_SPEC, MS_SCHEMA) == ("clean", set())


@pytest.mark.parametrize("name", INT_LEAKY)
def test_an_integer_time_bound_that_reaches_the_prediction_is_flagged(name):
    bound, rule = INT_LEAKY[name]
    verdict, rules = check(EVENTS_SUM.format(bound=bound), MS_SPEC, MS_SCHEMA)
    assert verdict == "leaky" and rule in rules


def test_a_constant_shift_of_an_integer_time_without_a_unit_is_not_read_as_a_bound():
    spec = {k: v for k, v in MS_SPEC.items() if k != "time_units"}
    sql = EVENTS_SUM.format(bound=f"e.event_ts < l.ts - {DAY_MS}")
    verdict, rules = check(sql, spec, MS_SCHEMA)
    assert verdict == "leaky" and "L3" in rules


MIXED_UNITS = {
    **MS_SPEC,
    "time_units": {"train": "milliseconds", "events": "seconds"},
}


@pytest.mark.parametrize(
    "bound",
    [f"e.event_ts < l.ts - {DAY_MS}", "e.event_ts < l.ts", f"l.ts - e.event_ts > {DAY_MS}"],
)
def test_two_integer_columns_in_different_units_do_not_bound_each_other(bound):
    verdict, rules = check(EVENTS_SUM.format(bound=bound), MIXED_UNITS, MS_SCHEMA)
    assert verdict == "leaky" and "L3" in rules


@pytest.mark.parametrize(
    "unit, day",
    [("seconds", 86_400), ("milliseconds", DAY_MS), ("microseconds", DAY_MS * 1000)],
)
def test_each_unit_scales_the_constant_to_seconds(unit, day):
    spec = {**MS_SPEC, "time_units": {"train": unit, "events": unit}}
    back = EVENTS_SUM.format(bound=f"e.event_ts < l.ts - {day}")
    ahead = EVENTS_SUM.format(bound=f"e.event_ts < l.ts + {day}")
    assert check(back, spec, MS_SCHEMA) == ("clean", set())
    assert check(ahead, spec, MS_SCHEMA)[1] == {"L1"}
    one_unit = EVENTS_SUM.format(bound="e.event_ts < l.ts + 1")
    assert check(one_unit, spec, MS_SCHEMA)[1] == {"L5"}


def test_the_shift_is_followed_through_a_cte_that_renames_the_column():
    sql = f"""WITH sp AS (SELECT user_id, ts AS cutoff, ans FROM train)
SELECT sp.user_id, sp.cutoff AS ts, sp.ans,
  (SELECT SUM(e.amount) FROM events e
   WHERE e.user_id = sp.user_id AND e.event_ts < sp.cutoff - {DAY_MS}) AS spend
FROM sp"""
    assert check(sql, MS_SPEC, MS_SCHEMA) == ("clean", set())


# --- windows that read only the labels of earlier rows -------------------------------------------

KEY_PART = "PARTITION BY user_id ORDER BY ts"
SEL_PLAYS = "SELECT user_id, ts, correct"


def windowed(expr: str, source: str = "plays") -> str:
    return f"{SEL_PLAYS}, {expr} AS feat FROM {source}"


def over(func: str, frame: str = "", partition: str = "PARTITION BY user_id", order: str = "ts"):
    return f"{func} OVER ({partition} ORDER BY {order}{' ' + frame if frame else ''})"


PAST_CLEAN = {
    "previous label": windowed(over("LAG(correct, 1)")),
    "label seven rows back": windowed(over("LAG(correct, 7)")),
    "previous label, offset left to its default": windowed(over("LAG(correct)")),
    "previous label with a constant default": windowed(over("LAG(correct, 1, 0)")),
    "running rate that stops one row before": windowed(
        over("AVG(correct)", "ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING")
    ),
    "trailing sum that stops two rows before": windowed(
        over("SUM(correct)", "ROWS BETWEEN 7 PRECEDING AND 2 PRECEDING")
    ),
    "first label of the history": windowed(
        over("FIRST_VALUE(correct)", "ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING")
    ),
    "the window over a finer partition": windowed(
        over("LAG(correct, 1)", partition="PARTITION BY user_id, task")
    ),
    "a filter on the rows the window reads": (
        f"{SEL_PLAYS}, {over('LAG(correct, 1)')} AS feat FROM plays WHERE task = 0"
    ),
    "range that ends an interval before the row": windowed(
        over("AVG(correct)", "RANGE BETWEEN INTERVAL 7 DAY PRECEDING AND INTERVAL 1 DAY PRECEDING")
    ),
    "a named window": (
        f"{SEL_PLAYS}, AVG(correct) OVER w AS feat FROM plays WINDOW w AS "
        "(PARTITION BY user_id ORDER BY ts ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING)"
    ),
    "through a cte": (
        f"WITH p AS (SELECT * FROM plays) {SEL_PLAYS}, {over('LAG(correct, 1)')} AS feat FROM p"
    ),
}
PAST_LEAKY = {
    "frame reaches the current row": windowed(
        over("AVG(correct)", "ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW")
    ),
    "trailing frame whose end is left to the default": windowed(
        over("AVG(correct)", "ROWS 3 PRECEDING")
    ),
    "default frame includes the row": windowed(over("AVG(correct)")),
    "frame looks ahead": windowed(over("AVG(correct)", "ROWS BETWEEN 1 PRECEDING AND 1 FOLLOWING")),
    "next label": windowed(over("LEAD(correct, 1)")),
    "lag by zero rows is the row itself": windowed(over("LAG(correct, 0)")),
    "lag by a negative number reads ahead": windowed(over("LAG(correct, -1)")),
    "lag by a column": windowed(over("LAG(correct, task)")),
    "label as the default of the lag": windowed(over("LAG(correct, 1, correct)")),
    "descending order reads later rows": windowed(over("LAG(correct, 1)", order="ts DESC")),
    "ties broken by another column": windowed(
        over(
            "AVG(correct)",
            "ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING",
            order="ts, task",
        )
    ),
    "ordered by something that is not the prediction time": windowed(
        over("LAG(correct, 1)", order="task")
    ),
    "ordered by the day of the prediction time, which ties rows of one key": windowed(
        over("LAG(correct, 1)", order="CAST(ts AS DATE)")
    ),
    "ordered by the label": windowed(over("LAG(correct, 1)", order="correct")),
    "no partition while the spec has a key": windowed(over("LAG(correct, 1)", partition="")),
    "previous label plus the label itself": windowed(over("LAG(correct, 1)") + " + correct"),
    "label inside the partition": windowed(
        over("LAG(correct, 1)", partition="PARTITION BY user_id, correct")
    ),
    "a window over a join": (
        "SELECT p.user_id, p.ts, p.correct, "
        + over("LAG(p.correct, 1)", partition="PARTITION BY p.user_id", order="p.ts")
        + " AS feat FROM plays p JOIN plays q ON q.user_id = p.user_id AND q.ts = p.ts"
    ),
    "rows frame of a range": windowed(
        over("AVG(correct)", "RANGE BETWEEN INTERVAL 7 DAY PRECEDING AND CURRENT ROW")
    ),
    "an aggregate that is not windowed": (
        f"{SEL_PLAYS}, (SELECT AVG(h.correct) FROM plays h "
        "WHERE h.user_id = plays.user_id) AS feat FROM plays"
    ),
}


@pytest.mark.parametrize("name", PAST_CLEAN)
def test_a_window_over_earlier_labels_is_not_a_use_of_the_target(name):
    assert check(PAST_CLEAN[name], with_delay("0 seconds"), PLAYS_SCHEMA) == ("clean", set())


@pytest.mark.parametrize("name", PAST_LEAKY)
def test_a_window_that_can_reach_the_row_or_a_later_one_is_still_a_use_of_the_target(name):
    verdict, rules = check(PAST_LEAKY[name], with_delay("0 seconds"), PLAYS_SCHEMA)
    assert verdict == "leaky" and "L6" in rules


PREV = """WITH h AS (SELECT user_id, ts, correct,
  LAG(correct) OVER (PARTITION BY user_id ORDER BY ts) AS prev FROM plays)
SELECT user_id, ts, correct, {expr} AS feat FROM h"""
PREV_DERIVED = """SELECT user_id, ts, correct, {expr} AS feat FROM (
  SELECT user_id, ts, correct,
    LAG(correct) OVER (PARTITION BY user_id ORDER BY ts) AS prev FROM plays) h"""
PREV_CHAINED = """WITH h AS (SELECT user_id, ts, correct,
  LAG(correct) OVER (PARTITION BY user_id ORDER BY ts) AS prev FROM plays),
h2 AS (SELECT user_id, ts, correct, prev AS shifted FROM h)
SELECT user_id, ts, correct, {expr} AS feat FROM h2"""

# the value of an accepted window reaches the output row by row; read across rows it brings the
# labels of later rows back onto earlier ones
FED_ON = {
    "sum over the whole partition beside a running sum": (
        PREV,
        "SUM(prev) OVER (PARTITION BY user_id) - SUM(prev) OVER (PARTITION BY user_id ORDER BY ts)",
    ),
    "maximum over the partition": (PREV, "MAX(prev) OVER (PARTITION BY user_id)"),
    "maximum over everything": (PREV, "MAX(prev) OVER ()"),
    "average over the partition": (PREV, "AVG(prev) OVER (PARTITION BY user_id)"),
    "rank by the shifted label": (PREV, "RANK() OVER (PARTITION BY user_id ORDER BY prev)"),
    "quartile by the shifted label": (PREV, "NTILE(4) OVER (PARTITION BY user_id ORDER BY prev)"),
    "the same through a derived table": (
        PREV_DERIVED,
        "SUM(prev) OVER (PARTITION BY user_id) - SUM(prev) OVER (PARTITION BY user_id ORDER BY ts)",
    ),
    "the same after a second cte": (
        PREV_CHAINED,
        "MAX(shifted) OVER (PARTITION BY user_id)",
    ),
}
FED_ROW_BY_ROW = {
    "passed through": (PREV, "prev"),
    "scaled and shifted": (PREV, "prev * 2 + 1"),
    "through a second cte": (PREV_CHAINED, "shifted"),
    "in a derived table": (PREV_DERIVED, "COALESCE(prev, 0)"),
}


@pytest.mark.parametrize("name", FED_ON)
def test_the_value_of_a_window_over_earlier_labels_is_not_read_across_rows(name):
    template, expr = FED_ON[name]
    verdict, rules = check(template.format(expr=expr), with_delay("0 seconds"), PLAYS_SCHEMA)
    assert verdict == "leaky" and "L6" in rules


@pytest.mark.parametrize("name", FED_ROW_BY_ROW)
def test_the_value_of_a_window_over_earlier_labels_may_be_used_row_by_row(name):
    template, expr = FED_ROW_BY_ROW[name]
    sql = template.format(expr=expr)
    assert check(sql, with_delay("0 seconds"), PLAYS_SCHEMA) == ("clean", set())


def test_a_downstream_filter_on_the_value_of_a_window_over_earlier_labels_is_fine():
    sql = PREV.format(expr="prev") + " WHERE prev = 1 ORDER BY ts"
    assert check(sql, with_delay("0 seconds"), PLAYS_SCHEMA) == ("clean", set())


ROW_MULTIPLIED = {
    "unnest in a cte": (
        "WITH c AS (SELECT user_id, ts, correct, UNNEST([1, 2]) AS x FROM plays) "
        f"{SEL_PLAYS}, {over('LAG(correct)')} AS feat FROM c"
    ),
    "unnest of a range": (
        "WITH c AS (SELECT user_id, ts, correct, UNNEST(range(2)) AS x FROM plays) "
        f"{SEL_PLAYS}, {over('AVG(correct)', 'ROWS BETWEEN 3 PRECEDING AND 1 PRECEDING')} "
        "AS feat FROM c"
    ),
    "unnest in a derived table": (
        f"{SEL_PLAYS}, {over('LAG(correct)')} AS feat FROM "
        "(SELECT user_id, ts, correct, UNNEST([1, 2]) AS x FROM plays) c"
    ),
    "unnest beside the grouped sum": (
        "WITH daily AS (SELECT user_id, ts, SUM(correct) AS total, UNNEST([1, 2]) AS x "
        "FROM plays GROUP BY user_id, ts) "
        f"SELECT user_id, ts, {over('LAG(total, 1)')} AS feat FROM daily"
    ),
}


@pytest.mark.parametrize("name", ROW_MULTIPLIED)
def test_rows_repeated_by_an_unnest_make_the_previous_row_the_row_itself(name):
    verdict, rules = check(ROW_MULTIPLIED[name], with_delay("0 seconds"), PLAYS_SCHEMA)
    assert verdict == "leaky" and "L6" in rules


WHOLE_ROW = {
    "the row as json": "to_json(d)",
    "the row as a value": "d",
    "a field of the row": "struct_extract(d, 'total')",
}


@pytest.mark.parametrize("name", WHOLE_ROW)
def test_a_whole_row_of_the_grouped_cte_carries_the_summed_label_beside_the_window(name):
    sql = f"""WITH daily AS (
  SELECT user_id, ts, SUM(correct) AS total FROM plays GROUP BY user_id, ts)
SELECT d.user_id, d.ts, {WHOLE_ROW[name]} AS whole,
  LAG(d.total, 1) OVER (PARTITION BY d.user_id ORDER BY d.ts) AS feat
FROM daily d"""
    verdict, rules = check(sql, with_delay("0 seconds"), PLAYS_SCHEMA)
    assert verdict == "leaky" and "L6" in rules


INCLUSIVE_ZERO = {**PLAYS_SPEC, "label_known_after": "0 seconds", "boundary": "inclusive"}


def test_with_an_inclusive_boundary_the_row_itself_is_still_not_an_earlier_label():
    sql = PRIOR_RATE.format(bound="h.ts <= l.ts")
    verdict, rules = check(sql, INCLUSIVE_ZERO, PLAYS_SCHEMA)
    assert verdict == "leaky" and "L6" in rules
    joined = """SELECT l.user_id, l.ts, l.correct, AVG(h.correct) AS prior_rate
FROM plays l LEFT JOIN plays h ON h.user_id = l.user_id AND h.ts <= l.ts
GROUP BY l.user_id, l.ts, l.correct"""
    verdict, rules = check(joined, INCLUSIVE_ZERO, PLAYS_SCHEMA)
    assert verdict == "leaky" and "L6" in rules


def test_with_an_inclusive_boundary_strictly_earlier_labels_are_still_known():
    sql = PRIOR_RATE.format(bound="h.ts < l.ts")
    assert check(sql, INCLUSIVE_ZERO, PLAYS_SCHEMA) == ("clean", set())
    back = PRIOR_RATE.format(bound="h.ts <= l.ts - INTERVAL 1 SECOND")
    assert check(back, INCLUSIVE_ZERO, PLAYS_SCHEMA) == ("clean", set())


@pytest.mark.parametrize("steps", ["1e309", "-1e309"])
def test_a_lag_offset_that_is_not_a_finite_number_is_not_a_crash(steps):
    sql = windowed(over(f"LAG(correct, {steps})"))
    report = validate(sql, LeakageSpec.from_dict(with_delay("0 seconds")), schema=PLAYS_SCHEMA)
    assert not [f for f in report.findings if f.rule_id == "PARSE"]
    assert report.verdict == "leaky"


@pytest.mark.parametrize("delay", [None, "1 hour", "14 days"])
@pytest.mark.parametrize("name", ["previous label", "trailing sum that stops two rows before"])
def test_earlier_labels_are_only_known_when_the_delay_is_zero(name, delay):
    spec = PLAYS_SPEC if delay is None else with_delay(delay)
    verdict, rules = check(PAST_CLEAN[name], spec, PLAYS_SCHEMA)
    assert verdict == "leaky" and "L6" in rules


KEYS_TWO = {**PLAYS_SPEC, "entity_keys": ["user_id", "task"], "label_known_after": "0 seconds"}


def test_a_window_partitioned_by_part_of_the_key_mixes_rows_stamped_alike():
    # (user_id, task, ts) is the row; ordering within user_id alone leaves rows of two tasks tied
    whole = windowed(over("LAG(correct, 1)", partition="PARTITION BY user_id, task"))
    part = windowed(over("LAG(correct, 1)", partition="PARTITION BY user_id"))
    assert check(whole, KEYS_TWO, PLAYS_SCHEMA) == ("clean", set())
    verdict, rules = check(part, KEYS_TWO, PLAYS_SCHEMA)
    assert verdict == "leaky" and "L6" in rules


def test_the_window_of_a_series_needs_no_partition():
    spec = {**SERIES_SPEC, "label_known_after": "0 seconds"}
    clean = "SELECT ts, target, LAG(target, 1) OVER (ORDER BY ts) AS prev FROM bars"
    leaky = "SELECT ts, target, LAG(target, 1) OVER (ORDER BY ts) + target AS prev FROM bars"
    assert check(clean, spec, SERIES_SCHEMA) == ("clean", set())
    assert check(leaky, spec, SERIES_SCHEMA)[1] == {"L6"}
    ahead = "SELECT ts, target, LEAD(target, 1) OVER (ORDER BY ts) AS nxt FROM bars"
    assert "L6" in check(ahead, spec, SERIES_SCHEMA)[1]


RANGE_ROWS = {
    "an interval longer than the delay": (
        "RANGE BETWEEN INTERVAL 30 DAY PRECEDING AND INTERVAL 15 DAY PRECEDING",
        "clean",
    ),
    "an interval shorter than the delay": (
        "RANGE BETWEEN INTERVAL 30 DAY PRECEDING AND INTERVAL 7 DAY PRECEDING",
        "leaky",
    ),
    "an interval equal to the delay": (
        "RANGE BETWEEN INTERVAL 30 DAY PRECEDING AND INTERVAL 14 DAY PRECEDING",
        "leaky",
    ),
    "a frame that ends at the row": (
        "RANGE BETWEEN INTERVAL 30 DAY PRECEDING AND CURRENT ROW",
        "leaky",
    ),
    "rows are not time": ("ROWS BETWEEN 30 PRECEDING AND 15 PRECEDING", "leaky"),
}


@pytest.mark.parametrize("name", RANGE_ROWS)
def test_a_range_window_is_known_once_its_end_is_older_than_the_label_delay(name):
    frame, expected = RANGE_ROWS[name]
    sql = windowed(over("AVG(correct)", frame))
    verdict, rules = check(sql, with_delay("14 days"), PLAYS_SCHEMA)
    assert verdict == ("clean" if expected == "clean" else "leaky")
    assert ("L6" in rules) == (expected == "leaky")


def test_an_interval_equal_to_the_delay_is_known_when_the_boundary_is_inclusive():
    sql = windowed(
        over(
            "AVG(correct)",
            "RANGE BETWEEN INTERVAL 30 DAY PRECEDING AND INTERVAL 14 DAY PRECEDING",
        )
    )
    spec = {**with_delay("14 days"), "boundary": "inclusive"}
    assert check(sql, spec, PLAYS_SCHEMA) == ("clean", set())


MS_PLAYS_SPEC = {
    **PLAYS_SPEC,
    "time_units": {"plays": "milliseconds"},
}
MS_PLAYS_SCHEMA = {"plays": {**PLAYS_SCHEMA["plays"], "ts": "BIGINT"}}
RANGE_MS = {
    "one millisecond before, no delay": (
        "RANGE BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING",
        "0 seconds",
        True,
    ),
    "row itself, no delay": (
        "RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW",
        "0 seconds",
        False,
    ),
    "a day and a millisecond, a day of delay": (
        "RANGE BETWEEN UNBOUNDED PRECEDING AND 86400001 PRECEDING",
        "1 day",
        True,
    ),
    "a second, a day of delay": (
        "RANGE BETWEEN UNBOUNDED PRECEDING AND 1000 PRECEDING",
        "1 day",
        False,
    ),
}


@pytest.mark.parametrize("name", RANGE_MS)
def test_a_range_over_an_integer_time_counts_its_offset_in_the_declared_unit(name):
    frame, delay, safe = RANGE_MS[name]
    sql = windowed(over("AVG(correct)", frame))
    verdict, rules = check(sql, {**MS_PLAYS_SPEC, "label_known_after": delay}, MS_PLAYS_SCHEMA)
    assert (verdict == "clean") is safe
    assert ("L6" in rules) is not safe


def test_a_range_offset_that_is_not_a_constant_proves_nothing():
    sql = windowed(over("AVG(correct)", "RANGE BETWEEN UNBOUNDED PRECEDING AND task PRECEDING"))
    for spec in ({**MS_PLAYS_SPEC, "label_known_after": "1 day"}, with_delay("0 seconds")):
        verdict, rules = check(sql, spec, MS_PLAYS_SCHEMA)
        assert verdict == "leaky" and "L6" in rules


def test_a_range_offset_in_an_unknown_unit_proves_nothing_against_a_positive_delay():
    sql = windowed(over("AVG(correct)", "RANGE BETWEEN UNBOUNDED PRECEDING AND 86400001 PRECEDING"))
    spec = {**PLAYS_SPEC, "label_known_after": "1 day"}
    assert check(sql, spec, MS_PLAYS_SCHEMA)[1] >= {"L6"}


# labels summed per row of the series first, then lagged (a daily total of a transaction label)
DAILY = """WITH daily AS (
  SELECT user_id, ts, SUM(correct) AS total FROM plays GROUP BY user_id, ts)
SELECT user_id, ts, {expr} AS feat FROM daily"""
GROUPED_CLEAN = {
    "daily total of a week ago": over("LAG(total, 7)"),
    "trailing total that stops a day before": over(
        "AVG(total)", "ROWS BETWEEN 7 PRECEDING AND 1 PRECEDING"
    ),
}
GROUPED_LEAKY = {
    "average that includes the day": over("AVG(total)", "ROWS BETWEEN 7 PRECEDING AND CURRENT ROW"),
    "growth against a week ago reads the day itself": f"total / {over('LAG(total, 7)')} - 1",
    "total passed through": "total",
    "day total used beside the lag": f"{over('LAG(total, 7)')} + total",
}


@pytest.mark.parametrize("name", GROUPED_CLEAN)
def test_a_label_summed_per_prediction_row_may_be_read_through_earlier_rows(name):
    sql = DAILY.format(expr=GROUPED_CLEAN[name])
    assert check(sql, with_delay("0 seconds"), PLAYS_SCHEMA) == ("clean", set())


@pytest.mark.parametrize("name", GROUPED_LEAKY)
def test_a_label_summed_per_prediction_row_is_still_the_target_on_the_row_itself(name):
    verdict, rules = check(
        DAILY.format(expr=GROUPED_LEAKY[name]), with_delay("0 seconds"), PLAYS_SCHEMA
    )
    assert verdict == "leaky" and "L6" in rules


GROUPED_NOT_UNIQUE = {
    "grouped finer than the row": (
        "WITH daily AS (SELECT user_id, ts, task, SUM(correct) AS total FROM plays "
        "GROUP BY user_id, ts, task) "
        f"SELECT user_id, ts, {over('LAG(total, 1)')} AS feat FROM daily"
    ),
    "grouped coarser than the row": (
        "WITH daily AS (SELECT user_id, MAX(ts) AS ts, SUM(correct) AS total FROM plays "
        "GROUP BY user_id) "
        f"SELECT user_id, ts, {over('LAG(total, 1)')} AS feat FROM daily"
    ),
    "not grouped, windowed over every row": (
        "WITH daily AS (SELECT user_id, ts, correct + 1 AS total FROM plays) "
        f"SELECT user_id, ts, {over('LAG(total, 1)')} AS feat FROM daily"
    ),
    "grouped but never read through a window": (
        "WITH daily AS (SELECT user_id, ts, SUM(correct) AS total FROM plays GROUP BY user_id, ts) "
        "SELECT user_id, ts, total AS feat FROM daily"
    ),
}


@pytest.mark.parametrize("name", GROUPED_NOT_UNIQUE)
def test_rows_that_are_not_one_per_key_and_time_stay_a_use_of_the_target(name):
    verdict, rules = check(GROUPED_NOT_UNIQUE[name], with_delay("0 seconds"), PLAYS_SCHEMA)
    assert verdict == "leaky" and "L6" in rules


# --- column lineage of a window over earlier labels ---------------------------------------------


def lineage_sources(sql: str, spec: dict, schema: dict) -> dict[str, set[str]]:
    from sqlguard.core.lineage import full_lineage
    from sqlguard.leakage.analysis import analyze

    parsed = LeakageSpec.from_dict(spec)
    analysis = analyze(sql, parsed, schema)
    tree, lineage_schema = analysis.lineage_input()
    found = full_lineage(tree, lineage_schema, parsed.dialect)
    return {c.lower(): {f"{s.table}.{s.column}".lower() for s in srcs} for c, srcs in found.items()}


def test_lineage_of_a_window_over_earlier_labels_has_no_label_source():
    sql = windowed(over("AVG(correct)", "ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING"))
    got = lineage_sources(sql, with_delay("0 seconds"), PLAYS_SCHEMA)
    assert "plays.correct" not in got["feat"]
    assert got["correct"] == {"plays.correct"}  # the passthrough label is still the label


def test_lineage_keeps_the_other_columns_a_window_over_earlier_labels_reads():
    sql = windowed(over("LAG(correct + task, 1)"))
    got = lineage_sources(sql, with_delay("0 seconds"), PLAYS_SCHEMA)
    assert "plays.task" in got["feat"] and "plays.correct" not in got["feat"]


def test_lineage_of_a_label_beside_the_window_is_untouched():
    sql = windowed(over("LAG(correct, 1)") + " + correct")
    assert "plays.correct" in lineage_sources(sql, with_delay("0 seconds"), PLAYS_SCHEMA)["feat"]


def test_neutralizing_the_window_for_lineage_leaves_the_analysed_tree_alone():
    from sqlguard.leakage.analysis import analyze

    sql = windowed(over("LAG(correct, 1)"))
    analysis = analyze(sql, LeakageSpec.from_dict(with_delay("0 seconds")), PLAYS_SCHEMA)
    before = analysis.qualified.sql()
    analysis.lineage_input()
    analysis.lineage_input()
    assert analysis.qualified.sql() == before
    assert analysis.past_label_args and not analysis.label_uses
