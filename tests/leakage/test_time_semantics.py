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
