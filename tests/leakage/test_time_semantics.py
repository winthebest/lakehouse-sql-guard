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


