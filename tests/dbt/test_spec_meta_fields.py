"""dbt `meta.leakage` for the optional spec fields: output_roles, time_unit, single series, a
label table that doubles as history."""

import pytest

from sqlguard.dbt.check import check_model
from sqlguard.dbt.inline import inline
from sqlguard.dbt.manifest import parse_manifest
from sqlguard.dbt.spec import build_spec, derive_spec, merge_spec
from sqlguard.leakage.spec import LeakageSpec, SpecError
from tests.dbt.helpers import (
    BASE,
    EVENTS,
    EVENTS_ID,
    LABELS,
    LABELS_ID,
    SPINE,
    TRAINING,
    columns,
    manifest,
    model,
    source,
)

SQL = (
    "select l.user_id, l.label_ts, l.churned, l.dbt_updated_at, count(e.event_id) n "
    'from "db"."raw"."labels" l left join "db"."raw"."events" e on e.user_id = l.user_id '
    "and e.event_ts < l.label_ts and e.ingested_at < l.label_ts group by 1, 2, 3, 4"
)
ROLES = {"label": ["churned"], "metadata": ["dbt_updated_at"]}
UNKNOWN_ROLES = {"label": ["nope"]}


def train(leakage=TRAINING, sql=SQL, deps=(LABELS_ID, EVENTS_ID)):
    return model("train", sql, list(deps), leakage)


def built(*entries, override=None, base=BASE):
    found = parse_manifest(manifest(*base, *entries))
    node = found.get("model.proj.train")
    return build_spec(inline(found, node), found.dialect, override)


def spine_with(**extra):
    return source("labels", {**SPINE, **extra}, LABELS)


def base_with(spine):
    return [spine, BASE[1], BASE[2]]


# -- output_roles ------------------------------------------------------------------------------


def test_output_roles_on_the_training_model_reach_the_spec():
    spec = built(train({**TRAINING, "output_roles": ROLES})).spec
    assert spec.output_roles.label == ("churned",)
    assert spec.output_roles.metadata == ("dbt_updated_at",)
    assert spec.output_roles.features is None
    assert spec.is_non_feature_output("dbt_updated_at")


def test_output_roles_can_be_declared_on_the_spine():
    spine = spine_with(output_roles={"metadata": ["dbt_updated_at"]})
    spec = built(train(), base=base_with(spine)).spec
    assert spec.output_roles.metadata == ("dbt_updated_at",)


def test_the_training_model_wins_over_the_spine_for_output_roles_as_a_whole():
    spine = spine_with(output_roles={"metadata": ["from_spine"], "label": ["churned"]})
    spec = built(
        train({**TRAINING, "output_roles": {"metadata": ["dbt_updated_at"]}}),
        base=base_with(spine),
    ).spec
    assert spec.output_roles.metadata == ("dbt_updated_at",)
    assert spec.output_roles.label == ()


def test_features_list_makes_every_other_output_a_non_feature():
    spec = built(train({**TRAINING, "output_roles": {"features": ["n"]}})).spec
    assert spec.output_roles.features == ("n",)
    assert spec.is_non_feature_output("dbt_updated_at") and not spec.is_non_feature_output("n")


def test_label_columns_may_be_left_out_when_output_roles_names_the_label():
    spine = source(
        "labels",
        {
            "role": "label_spine",
            "entity_keys": ["user_id"],
            "prediction_ts": "label_ts",
            "output_roles": {"label": ["churned"]},
        },
        LABELS,
    )
    spec = built(train(), base=base_with(spine)).spec
    assert spec.label_columns == () and spec.output_roles.label == ("churned",)


def test_a_bad_output_roles_value_is_refused_with_the_model_named():
    with pytest.raises(SpecError, match=r"model\.proj\.train: output_roles has unknown roles"):
        built(train({**TRAINING, "output_roles": {"labels": ["churned"]}}))
    with pytest.raises(SpecError, match=r"model\.proj\.train: output_roles must be a mapping"):
        built(train({**TRAINING, "output_roles": ["churned"]}))
    with pytest.raises(SpecError, match="more than one role"):
        built(train({**TRAINING, "output_roles": {"label": ["a"], "metadata": ["a"]}}))
    with pytest.raises(SpecError, match="features must not be empty"):
        built(train({**TRAINING, "output_roles": {"features": []}}))
    with pytest.raises(SpecError, match="entity key or the prediction time"):
        built(train({**TRAINING, "output_roles": {"metadata": ["user_id"]}}))


def test_output_roles_in_the_spec_file_is_a_relaxation_and_is_warned_about():
    result = built(train(), override={"output_roles": ROLES})
    assert result.spec.output_roles.metadata == ("dbt_updated_at",)
    assert result.warnings == [
        "--spec sets output_roles[label] = ['churned'] (the dbt meta does not); this can "
        "relax a rule",
        "--spec sets output_roles[metadata] = ['dbt_updated_at'] (the dbt meta does not); this "
        "can relax a rule",
    ]


def test_the_spec_file_wins_over_the_meta_output_roles_and_new_columns_are_warned_about():
    meta = train({**TRAINING, "output_roles": {"metadata": ["dbt_updated_at"]}})
    result = built(meta, override={"output_roles": {"metadata": ["n"]}})
    assert result.spec.output_roles.metadata == ("n",)
    assert result.warnings == [
        "--spec sets output_roles[metadata] = n (the dbt meta does not); this can relax a rule",
        "--spec changes output_roles from dbt meta: "
        "{'metadata': ['dbt_updated_at']} -> {'metadata': ['n']}",
    ]


def test_the_same_output_roles_in_the_spec_file_and_the_meta_warn_of_nothing():
    meta = train({**TRAINING, "output_roles": {"metadata": ["dbt_updated_at"]}})
    assert built(meta, override={"output_roles": {"metadata": ["DBT_UPDATED_AT"]}}).warnings == []


def test_merge_spec_names_each_role_that_gains_a_column():
    merged, warnings = merge_spec(
        {"output_roles": {"label": ["a"], "metadata": ["m"]}},
        {"output_roles": {"label": ["a"], "metadata": ["m", "z"], "features": ["f"]}},
    )
    assert merged["output_roles"]["metadata"] == ["m", "z"]
    assert warnings == [
        "--spec sets output_roles[metadata] = z (the dbt meta does not); this can relax a rule",
        "--spec sets output_roles[features] = f (the dbt meta does not); this can relax a rule",
        "--spec changes output_roles from dbt meta: "
        "{'label': ['a'], 'metadata': ['m']} -> "
        "{'label': ['a'], 'metadata': ['m', 'z'], 'features': ['f']}",
    ]


# -- time_unit ---------------------------------------------------------------------------------


def events_with(**extra):
    return source(
        "events",
        {"time_column": "event_ts", "availability_column": "ingested_at", **extra},
        EVENTS,
    )


def test_time_unit_on_a_temporal_table_becomes_time_units():
    spec = built(train(), base=[BASE[0], events_with(time_unit="Milliseconds"), BASE[2]]).spec
    assert spec.time_units == {"events": "milliseconds"}


def test_time_unit_on_the_label_table_is_kept():
    spine = spine_with(time_column="label_ts", time_unit="seconds")
    spec = built(train(), base=base_with(spine)).spec
    assert spec.time_units == {"labels": "seconds"}


def test_a_table_without_time_unit_has_no_entry():
    assert built(train()).spec.time_units == {}


def test_time_unit_without_a_time_column_is_refused():
    spine = spine_with(time_unit="seconds")
    with pytest.raises(SpecError, match="time_unit needs a time_column"):
        built(train(), base=base_with(spine))


def test_an_unknown_time_unit_is_refused():
    with pytest.raises(SpecError, match=r"model\.proj\.train: time_units must be one of"):
        built(train(), base=[BASE[0], events_with(time_unit="days"), BASE[2]])
    with pytest.raises(SpecError, match="time_units must be one of"):
        built(train(), base=[BASE[0], events_with(time_unit=3), BASE[2]])


def test_time_units_in_the_spec_file_merge_per_table_and_a_change_is_warned_about():
    base = [BASE[0], events_with(time_unit="seconds"), BASE[2]]
    result = built(train(), base=base, override={"time_units": {"events": "milliseconds"}})
    assert result.spec.time_units == {"events": "milliseconds"}
    assert result.warnings == [
        "--spec changes time_units[events] from dbt meta: seconds -> milliseconds"
    ]
    assert built(train(), base=base, override={"time_units": {"EVENTS": "Seconds"}}).warnings == []
    added = built(train(), override={"time_units": {"events": "seconds"}})
    assert added.spec.time_units == {"events": "seconds"} and added.warnings == []


# -- validity_end_column and unique_keys -------------------------------------------------------


def test_validity_end_column_on_a_temporal_table_becomes_validity_end_columns():
    events = events_with(validity_end_column="Valid_To")
    spec = built(train(), base=[BASE[0], events, BASE[2]]).spec
    assert spec.validity_end_columns == {"events": "valid_to"}


def test_validity_end_column_without_a_time_column_is_refused():
    spine = spine_with(validity_end_column="valid_to")
    with pytest.raises(SpecError, match="validity_end_column needs a time_column"):
        built(train(), base=base_with(spine))


def test_a_table_without_validity_end_column_has_no_entry():
    assert built(train()).spec.validity_end_columns == {}


def test_unique_keys_on_the_label_spine_reach_the_spec():
    for declared, expected in (
        (["user_id"], (("user_id",),)),
        ([["user_id"], ["a", "b"]], (("user_id",), ("a", "b"))),
    ):
        spine = spine_with(unique_keys=declared)
        assert built(train(), base=base_with(spine)).spec.unique_keys == {"labels": expected}
    assert built(train()).spec.unique_keys == {}


def test_a_malformed_unique_keys_value_is_refused_with_the_model_named():
    spine = spine_with(unique_keys="user_id")
    with pytest.raises(SpecError, match=r"model\.proj\.train: unique_keys"):
        built(train(), base=base_with(spine))


def test_validity_and_unique_keys_in_the_spec_file_are_relaxations_and_are_warned_about():
    result = built(
        train(),
        override={"unique_keys": {"labels": ["user_id"]}, "validity_end_columns": {"events": "x"}},
    )
    assert result.spec.unique_keys == {"labels": (("user_id",),)}
    assert result.spec.validity_end_columns == {"events": "x"}
    assert sorted(w.split(" (")[0] for w in result.warnings) == [
        "--spec sets unique_keys[labels] = ['user_id']",
        "--spec sets validity_end_columns[events] = x",
    ]


# -- entity_keys: [] ---------------------------------------------------------------------------


def test_an_empty_entity_keys_list_is_a_declaration_not_a_gap():
    spine = spine_with(entity_keys=[])
    spec = built(train(), base=base_with(spine)).spec
    assert spec.entity_keys == ()


def test_an_empty_entity_keys_on_the_training_model_beats_the_spine_keys():
    spec = built(train({**TRAINING, "entity_keys": []})).spec
    assert spec.entity_keys == ()


def test_entity_keys_stay_required():
    spine = source(
        "labels",
        {k: v for k, v in SPINE.items() if k != "entity_keys"},
        LABELS,
    )
    with pytest.raises(SpecError, match=r"entity_keys \(write \[\] for a single series\)"):
        built(train(), base=base_with(spine))


# -- label_known_after: 0 ----------------------------------------------------------------------


def test_zero_label_known_after_is_a_declaration():
    for zero in (0, "0", 0.0):
        spec = built(train({**TRAINING, "label_known_after": zero})).spec
        assert spec.label_known_after == 0.0


def test_a_zero_ingest_delay_is_still_refused():
    events = events_with(max_ingest_delay=0)
    with pytest.raises(SpecError, match="max_ingest_delay"):
        built(train(), base=[BASE[0], events, BASE[2]])


# -- the label table as history ----------------------------------------------------------------


def test_a_label_spine_with_a_time_column_is_also_a_temporal_table():
    spine = spine_with(time_column="label_ts", availability_column="label_ts")
    result = built(train(), base=base_with(spine))
    spec = result.spec
    assert spec.label_table == "labels"
    assert spec.time_columns == {"labels": "label_ts", "events": "event_ts"}
    assert spec.availability_columns["labels"] == "label_ts"
    assert spec.classify("labels") == "label"
    assert result.gaps == [] and result.warnings == []


def test_an_outcome_column_on_the_label_table_marks_only_that_column():
    spine = spine_with(time_column="label_ts", outcome_columns=["churned"])
    spec = built(train(), base=base_with(spine)).spec
    assert [(c.table, c.column) for c in spec.outcome_columns] == [("labels", "churned")]
    assert spec.outcome_only_tables == set()  # a label table is read for its other columns


def test_the_label_table_alone_is_enough_history_to_check():
    sql = (
        "select l.user_id, l.label_ts, l.churned, count(*) over (partition by l.user_id "
        "order by l.label_ts rows between 3 preceding and 1 preceding) n "
        'from "db"."raw"."labels" l'
    )
    spine = spine_with(time_column="label_ts", time_unit="seconds")
    spec = built(train(sql=sql, deps=(LABELS_ID,)), base=[spine]).spec
    assert set(spec.time_columns) == {"labels"}


# -- a whole project: history spine, derived label, run metadata ------------------------------


def history_project():
    labels = columns(
        user_id="bigint", label_ts="timestamp", churned="boolean", dbt_updated_at="timestamp"
    )
    spine = source(
        "labels",
        {
            **SPINE,
            "time_column": "label_ts",
            "time_unit": "seconds",
            "label_known_after": 0,
            "output_roles": {"metadata": ["dbt_updated_at"]},
        },
        labels,
    )
    return [spine, BASE[1]]


def test_a_history_spine_with_roles_and_zero_delay_gives_the_whole_spec():
    found = parse_manifest(manifest(*history_project(), train()))
    node = found.get("model.proj.train")
    derived = derive_spec(inline(found, node), found.dialect)
    assert derived["time_columns"] == {"labels": "label_ts", "events": "event_ts"}
    assert derived["time_units"] == {"labels": "seconds"}
    assert derived["label_known_after"] == 0
    assert derived["output_roles"] == {"metadata": ["dbt_updated_at"]}
    spec = LeakageSpec.from_dict(derived)
    assert spec.label_known_after == 0.0 and spec.output_roles.metadata == ("dbt_updated_at",)


def test_the_same_project_checks_end_to_end_without_a_crash():
    found = parse_manifest(manifest(*history_project(), train()))
    result = check_model(found, found.get("model.proj.train"))
    assert result.report.verdict in {"clean", "leaky", "unverifiable"}
    assert not [f for f in result.report.findings if f.rule_id == "SPEC"]
