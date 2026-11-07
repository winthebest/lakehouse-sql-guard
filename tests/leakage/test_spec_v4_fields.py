"""The optional spec fields for single-series data, label tables that double as history, integer
time units, zero label delay and output-column roles. A spec that uses none of them is unchanged."""

import json
from dataclasses import asdict, fields
from pathlib import Path

import pytest
import yaml
from sqlglot import exp

from sqlguard.core.lineage import SourceColumn
from sqlguard.leakage.analysis import ClockUse, LabelUse, analyze
from sqlguard.leakage.spec import LeakageSpec, SpecError, parse_duration

from .conftest import SCHEMA, SPEC

ROOT = Path(__file__).resolve().parents[2]
RECORDED = Path(__file__).parent / "data" / "spec-objects-v3.json"
NEW_FIELDS = {"time_units", "output_roles"}


def spec_with(**overrides) -> LeakageSpec:
    return LeakageSpec.from_dict({**SPEC, **overrides})


def without(*keys: str) -> dict:
    return {k: v for k, v in SPEC.items() if k not in keys}


def plain(value):
    return json.loads(json.dumps(value, default=list))


# --- specs written before these fields existed -------------------------------------------------


def test_every_recorded_spec_has_a_file():
    recorded = json.loads(RECORDED.read_text(encoding="utf-8"))
    assert len(recorded) == 22
    assert all((ROOT / path).is_file() for path in recorded)


@pytest.mark.parametrize("path", sorted(json.loads(RECORDED.read_text(encoding="utf-8"))))
def test_a_spec_without_the_new_fields_loads_to_the_object_it_always_did(path):
    expected = json.loads(RECORDED.read_text(encoding="utf-8"))[path]
    loaded = plain(asdict(LeakageSpec.from_yaml(ROOT / path)))
    assert loaded.pop("time_units") == {}
    assert loaded.pop("output_roles") == {"label": [], "metadata": [], "features": None}
    assert loaded == expected


def test_the_new_fields_default_to_nothing_declared():
    spec = LeakageSpec.from_dict(SPEC)
    assert spec.time_units == {}
    assert spec.output_roles.label == () and spec.output_roles.metadata == ()
    assert spec.output_roles.features is None
    assert {f.name for f in fields(spec)} >= NEW_FIELDS


# --- the Kaggle specs, byte for byte ----------------------------------------------------------

KAGGLE = sorted((ROOT / "bench" / "kaggle" / "cases").glob("*/spec.yaml"))


def test_there_are_fourteen_kaggle_specs():
    assert len(KAGGLE) == 14


@pytest.mark.parametrize("path", KAGGLE, ids=lambda p: p.parent.name)
def test_every_kaggle_spec_loads(path):
    spec = LeakageSpec.from_yaml(path)
    assert spec.label_table not in spec.static_tables


def test_a_single_series_has_no_entity_key():
    for case in ("kl03", "kl16"):
        spec = LeakageSpec.from_yaml(ROOT / "bench" / "kaggle" / "cases" / case / "spec.yaml")
        assert spec.entity_keys == () and spec.key_columns == [spec.prediction_ts]


def test_a_zero_label_delay_without_a_unit_is_read_as_zero_seconds():
    spec = LeakageSpec.from_yaml(ROOT / "bench" / "kaggle" / "cases" / "kl06" / "spec.yaml")
    assert spec.label_known_after == 0 and spec.label_known_after is not None
    assert spec.label_table in spec.time_columns
    assert spec.outcome_columns == (SourceColumn("train", "user_answer"),)


# --- entity_keys ------------------------------------------------------------------------------


def test_entity_keys_may_be_an_empty_list():
    spec = spec_with(entity_keys=[])
    assert spec.entity_keys == () and spec.passthrough_columns == {"label_ts", "churned"}


@pytest.mark.parametrize("value", [None, "user_id", [""], [3], {"a": 1}])
def test_entity_keys_must_still_be_a_list_of_names(value):
    with pytest.raises(SpecError):
        spec_with(entity_keys=value)


def test_entity_keys_may_not_be_left_out():
    with pytest.raises(SpecError, match=r"missing required fields: entity_keys \(write \[\]"):
        LeakageSpec.from_dict(without("entity_keys"))


def test_other_lists_are_still_non_empty():
    with pytest.raises(SpecError, match="static_tables"):
        spec_with(static_tables=[1])
    with pytest.raises(SpecError, match="missing required fields: time_columns"):
        spec_with(time_columns={})


# --- label_columns and output_roles.label ------------------------------------------------------


def test_label_columns_may_be_left_out_when_a_label_role_is_declared():
    spec = LeakageSpec.from_dict({**without("label_columns"), "output_roles": {"label": ["Y"]}})
    assert spec.label_columns == () and spec.output_roles.label == ("y",)
    assert "y" in spec.passthrough_columns and spec.target_sources == {
        SourceColumn("user_outcomes", "refund_after_churn")
    }


def test_an_empty_label_columns_list_is_fine_beside_a_label_role():
    spec = LeakageSpec.from_dict(
        {**SPEC, "label_columns": [], "output_roles": {"label": ["next_day_return"]}}
    )
    assert spec.label_columns == ()


@pytest.mark.parametrize("extra", [{}, {"output_roles": {"metadata": ["built_at"]}}])
def test_some_label_must_be_named(extra):
    with pytest.raises(SpecError, match=r"missing required fields: label_columns \(or output_ro"):
        LeakageSpec.from_dict({**without("label_columns"), **extra})
    with pytest.raises(SpecError, match="label_columns"):
        LeakageSpec.from_dict({**SPEC, "label_columns": [], **extra})


def test_label_columns_given_as_a_string_is_rejected_even_with_a_label_role():
    with pytest.raises(SpecError, match="label_columns"):
        LeakageSpec.from_dict({**SPEC, "label_columns": "y", "output_roles": {"label": ["y"]}})


def test_a_label_role_adds_to_the_passthrough_columns():
    spec = spec_with(output_roles={"label": ["Next_Day_Return"], "metadata": ["built_at"]})
    assert spec.passthrough_columns == {"user_id", "label_ts", "churned", "next_day_return"}


# --- the label table as history ----------------------------------------------------------------


def test_the_label_table_may_be_a_temporal_table():
    spec = spec_with(time_columns={**SPEC["time_columns"], "labels": "label_ts"})
    assert spec.classify("labels") == "label"
    assert spec.time_columns["labels"] == spec.prediction_ts


def test_the_label_table_may_have_a_time_column_of_its_own():
    spec = spec_with(
        time_columns={"labels": "event_ts"},
        availability_columns={"labels": "loaded_at"},
        max_ingest_delay={"labels": "1 day"},
        time_units={"labels": "milliseconds"},
    )
    assert spec.availability_columns == {"labels": "loaded_at"}
    assert spec.max_ingest_delay == {"labels": 86_400.0}
    assert spec.time_units == {"labels": "milliseconds"}


def test_the_label_table_may_still_not_be_static():
    with pytest.raises(SpecError, match="label_table must not also be declared static"):
        spec_with(static_tables=["labels"])
    with pytest.raises(SpecError, match="label_table must not also be declared static"):
        spec_with(
            static_tables=["labels"], time_columns={**SPEC["time_columns"], "labels": "label_ts"}
        )


def test_other_tables_may_still_not_be_both_static_and_temporal():
    with pytest.raises(SpecError, match="both static and temporal"):
        spec_with(static_tables=["events"])


def test_outcome_only_tables_leave_out_label_temporal_and_static_tables():
    spec = spec_with(
        time_columns={**SPEC["time_columns"], "labels": "label_ts"},
        outcome_columns=[
            "labels.refund",
            "events.refund",
            "entities.refund",
            "user_outcomes.refund_after_churn",
        ],
    )
    assert spec.outcome_tables == {"labels", "events", "entities", "user_outcomes"}
    assert spec.outcome_only_tables == {"user_outcomes"}


def test_an_outcome_column_may_point_at_the_prediction_time():
    spec = spec_with(outcome_columns=["labels.label_ts"])
    assert spec.outcome_columns == (SourceColumn("labels", "label_ts"),)
    assert spec.outcome_only_tables == set()


def test_the_outcome_table_set_keeps_its_meaning():
    spec = LeakageSpec.from_dict(SPEC)
    assert spec.outcome_tables == {"user_outcomes"} == spec.outcome_only_tables
    assert spec.classify("user_outcomes") == "outcome"


# --- durations ---------------------------------------------------------------------------------


@pytest.mark.parametrize("value", [0, 0.0, "0", "0 seconds", "0 days", " 0 minutes "])
def test_label_known_after_accepts_zero(value):
    assert spec_with(label_known_after=value).label_known_after == 0.0


def test_label_known_after_still_defaults_to_not_declared():
    assert LeakageSpec.from_dict(SPEC).label_known_after is None
    assert spec_with(label_known_after="2 days").label_known_after == 172_800.0


@pytest.mark.parametrize("value", ["-1 days", -1, "0 months", "zero", True, False, [0], "1e3"])
def test_label_known_after_rejects_other_values(value):
    with pytest.raises(SpecError, match="label_known_after"):
        spec_with(label_known_after=value)


@pytest.mark.parametrize("value", [0, "0", "0 seconds"])
def test_max_ingest_delay_stays_positive(value):
    with pytest.raises(SpecError, match="positive duration"):
        spec_with(max_ingest_delay={"events": value})


def test_zero_is_only_allowed_when_asked_for():
    assert parse_duration("0 days", "x", allow_zero=True) == 0
    assert parse_duration(0, "x", allow_zero=True) == 0
    with pytest.raises(SpecError, match="non-negative duration"):
        parse_duration("-1 days", "x", allow_zero=True)
    with pytest.raises(SpecError, match="positive duration"):
        parse_duration(0, "x")


# --- time_units --------------------------------------------------------------------------------


@pytest.mark.parametrize("unit", ["seconds", "milliseconds", "microseconds"])
def test_time_units_accept_the_three_units(unit):
    assert spec_with(time_units={"events": unit}).time_units == {"events": unit}


def test_time_units_are_case_folded():
    spec = spec_with(time_units={"Events": "Milliseconds"})
    assert spec.time_units == {"events": "milliseconds"}


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ({"events": "minutes"}, "seconds, milliseconds or microseconds"),
        ({"events": "second"}, "seconds, milliseconds or microseconds"),
        ({"events": 1000}, "seconds, milliseconds or microseconds"),
        ({"entities": "seconds"}, "tables without a time column"),
        (["events"], "mapping"),
        ("seconds", "mapping"),
    ],
)
def test_bad_time_units_are_rejected(value, message):
    with pytest.raises(SpecError, match=message):
        spec_with(time_units=value)


def test_time_units_may_be_left_empty():
    assert spec_with(time_units=None).time_units == {}
    assert spec_with(time_units={}).time_units == {}


# --- output_roles ------------------------------------------------------------------------------


def test_output_roles_are_stored_lowercase():
    spec = spec_with(
        output_roles={"label": ["Next_Return"], "metadata": ["Built_At"], "features": ["Ret_5d"]}
    )
    roles = spec.output_roles
    assert (roles.label, roles.metadata, roles.features) == (
        ("next_return",),
        ("built_at",),
        ("ret_5d",),
    )


def test_each_role_may_be_left_out():
    assert spec_with(output_roles={"metadata": ["built_at"]}).output_roles.label == ()
    assert spec_with(output_roles={"label": ["y"]}).output_roles.features is None
    assert spec_with(output_roles=None).output_roles.metadata == ()
    assert spec_with(output_roles={}).output_roles.features is None


@pytest.mark.parametrize(
    ("roles", "message"),
    [
        ("label", "output_roles must be a mapping"),
        (["label"], "output_roles must be a mapping"),
        ({"labels": ["y"]}, "output_roles has unknown roles"),
        ({"label": "y"}, "output_roles.label must be a list"),
        ({"metadata": [1]}, "output_roles.metadata"),
        ({"features": ["a", ""]}, "output_roles.features"),
        ({"features": []}, "output_roles.features must not be empty"),
        ({"label": ["y"], "metadata": ["y"]}, "more than one role"),
        ({"metadata": ["a"], "features": ["A"]}, "more than one role"),
        ({"label": ["user_id"]}, "entity key or the prediction time"),
        ({"metadata": ["Label_TS"]}, "entity key or the prediction time"),
        ({"features": ["label_ts", "x"]}, "entity key or the prediction time"),
    ],
)
def test_bad_output_roles_are_rejected(roles, message):
    with pytest.raises(SpecError, match=message):
        spec_with(output_roles=roles)


def test_non_feature_outputs_are_the_label_and_metadata_roles():
    spec = spec_with(output_roles={"label": ["y"], "metadata": ["built_at"]})
    assert spec.non_feature_outputs == {"y", "built_at"}
    assert spec.is_non_feature_output("Y") and spec.is_non_feature_output("built_at")
    assert not spec.is_non_feature_output("ret_5d")


def test_a_features_list_makes_every_other_output_a_non_feature():
    spec = spec_with(output_roles={"features": ["ret_5d", "vol_20d"], "metadata": ["built_at"]})
    assert spec.non_feature_outputs == {"built_at"}
    assert not spec.is_non_feature_output("RET_5D")
    assert spec.is_non_feature_output("anything_else")
    assert spec.is_non_feature_output("built_at")


def test_without_roles_no_output_is_a_non_feature():
    spec = LeakageSpec.from_dict(SPEC)
    assert spec.non_feature_outputs == set()
    assert not spec.is_non_feature_output("whatever")


def test_a_hand_built_spec_is_checked_like_a_loaded_one():
    from sqlguard.leakage.spec import OutputRoles

    base = LeakageSpec.from_dict(SPEC)
    fields_ = {f.name: getattr(base, f.name) for f in fields(base)}
    with pytest.raises(SpecError, match="more than one role"):
        LeakageSpec(**{**fields_, "output_roles": OutputRoles(label=("a",), metadata=("a",))})
    with pytest.raises(SpecError, match="label"):
        LeakageSpec(**{**fields_, "label_columns": ()})
    with pytest.raises(SpecError, match="time_units"):
        LeakageSpec(**{**fields_, "time_units": {"events": "hours"}})


# --- the spec file round trip -----------------------------------------------------------------


def test_a_spec_file_with_every_new_field_loads(tmp_path):
    path = tmp_path / "spec.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                **SPEC,
                "entity_keys": [],
                "time_columns": {"labels": "event_ms", "events": "event_ts"},
                "time_units": {"labels": "milliseconds"},
                "label_known_after": 0,
                "output_roles": {"label": ["y"], "metadata": ["built_at"], "features": ["f"]},
            }
        ),
        encoding="utf-8",
    )
    spec = LeakageSpec.from_yaml(path)
    assert spec.entity_keys == () and spec.label_known_after == 0
    assert spec.time_units == {"labels": "milliseconds"}
    assert spec.output_roles.features == ("f",)


# --- the nodes behind label and clock uses ----------------------------------------------------

SELECT = "SELECT l.user_id, l.label_ts, l.churned"


def test_a_label_use_carries_the_expression_it_came_from():
    analysis = analyze(
        f"{SELECT}, l.churned + 1 AS shifted FROM labels l", LeakageSpec.from_dict(SPEC), SCHEMA
    )
    (use,) = analysis.label_uses
    assert isinstance(use.node, exp.Column) and use.node.name == "churned"
    assert use.node.parent is not None and use.node.parent.sql() == use.sql


def test_a_clock_use_carries_the_function_it_came_from():
    analysis = analyze(
        f"{SELECT}, now() AS stamp FROM labels l", LeakageSpec.from_dict(SPEC), SCHEMA
    )
    (use,) = analysis.clock_uses
    assert isinstance(use.node, exp.Expression) and use.node.sql() == use.sql


def test_the_node_does_not_take_part_in_equality_or_repr():
    ref = analyze(f"{SELECT} FROM labels l", LeakageSpec.from_dict(SPEC), SCHEMA).refs[0]
    column = exp.column("churned")
    assert LabelUse(ref, "churned", "x", column) == LabelUse(ref, "churned", "x")
    assert ClockUse("now()", exp.Anonymous(this="now")) == ClockUse("now()")
    assert "Column" not in repr(LabelUse(ref, "churned", "x", column))
    assert "Anonymous" not in repr(ClockUse("now()", exp.Anonymous(this="now")))
