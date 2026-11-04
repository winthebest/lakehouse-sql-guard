import pytest

from sqlguard.dbt.inline import inline
from sqlguard.dbt.manifest import parse_manifest
from sqlguard.dbt.spec import build_spec, derive_spec, merge_spec
from sqlguard.leakage.spec import SpecError
from tests.dbt.helpers import (
    BASE,
    EVENTS,
    EVENTS_ID,
    LABELS,
    LABELS_ID,
    SPINE,
    TRAINING,
    USERS_ID,
    columns,
    load_fixture,
    manifest,
    model,
    source,
)

SQL = (
    'select l.user_id, l.label_ts, l.churned, count(e.event_id) n from "db"."raw"."labels" l '
    'left join "db"."raw"."events" e on e.user_id = l.user_id and e.event_ts < l.label_ts '
    "and e.ingested_at < l.label_ts group by 1, 2, 3"
)


def built(*entries, override=None, column_types=None, base=BASE):
    found = parse_manifest(manifest(*base, *entries))
    node = found.get("model.proj.train")
    return build_spec(inline(found, node), found.dialect, override, column_types)


def train(sql=SQL, deps=(LABELS_ID, EVENTS_ID), leakage=TRAINING):
    return model("train", sql, list(deps), leakage)


def test_spec_comes_from_the_meta_of_the_tables_the_query_reads():
    result = built(train())
    spec = result.spec
    assert (spec.label_table, spec.entity_keys, spec.prediction_ts) == (
        "labels",
        ("user_id",),
        "label_ts",
    )
    assert spec.label_columns == ("churned",)
    assert spec.time_columns == {"events": "event_ts"}
    assert spec.availability_columns == {"events": "ingested_at"}
    assert spec.relations == {"db.raw.labels": "raw.labels", "db.raw.events": "raw.events"}
    assert spec.dialect == "duckdb" and spec.boundary == "strict"
    assert result.schema["events"]["event_ts"] == "timestamp"
    assert result.warnings == [] and result.gaps == []


def test_a_training_set_that_reads_no_temporal_table_has_nothing_to_check():
    sql = 'select l.user_id, l.label_ts, l.churned from "db"."raw"."labels" l'
    with pytest.raises(
        SpecError, match=r"model\.proj\.train reads no table that declares.*\(it reads: labels\)"
    ):
        built(model("train", sql, [LABELS_ID], TRAINING))


def test_the_training_model_overrides_what_the_spine_declares():
    result = built(
        train(leakage={**TRAINING, "boundary": "inclusive", "label_known_after": "14 days"})
    )
    assert result.spec.boundary == "inclusive" and result.spec.label_known_after == 14 * 86400
    own = {**TRAINING, "prediction_ts": "label_ts", "label_columns": ["churned"]}
    assert built(train(leakage=own)).spec.label_columns == ("churned",)


def test_static_tables_outcomes_and_ingest_delay():
    users = source(
        "users",
        {"role": "static", "outcome_columns": ["flag"]},
        columns(user_id="bigint", flag="int"),
    )
    events = source(
        "events",
        {
            "time_column": "event_ts",
            "availability_column": "ingested_at",
            "max_ingest_delay": "2 days",
        },
        EVENTS,
    )
    sql = SQL.replace(
        "group by", 'left join "db"."raw"."users" u on u.user_id = l.user_id group by'
    )
    found = built(
        train(sql, (LABELS_ID, EVENTS_ID, USERS_ID)),
        base=[(LABELS_ID, BASE[0][1]), events, users],
    )
    assert found.spec.static_tables == ("users",)
    assert [(c.table, c.column) for c in found.spec.outcome_columns] == [("users", "flag")]
    assert found.spec.max_ingest_delay == {"events": 2 * 86400}


def test_names_with_quotes_and_capitals_are_normalised():
    sql = SQL.replace('"db"."raw"."labels"', '"DB"."Raw"."Labels"')
    data = manifest(*BASE, model("train", sql, [LABELS_ID, EVENTS_ID], TRAINING))
    data["sources"][LABELS_ID]["relation_name"] = '"DB"."Raw"."Labels"'
    found = parse_manifest(data)
    result = build_spec(inline(found, found.get("model.proj.train")), found.dialect)
    assert result.spec.label_table == "labels"
    assert result.spec.relations["db.raw.labels"] == "raw.labels"


def test_iceberg_table_names_the_catalog_table():
    events = source(
        "events",
        {"time_column": "event_ts", "availability_column": "ingested_at"},
        EVENTS,
        iceberg_table="{schema}.events_v2",
    )
    result = built(train(), base=[BASE[0], events, BASE[2]])
    assert result.spec.relations["db.raw.events"] == "raw.events_v2"
    assert result.spec.time_columns == {"events_v2": "event_ts"}
    assert "events_v2" in result.schema
    bad = source("events", {"time_column": "event_ts"}, EVENTS, iceberg_table="{nope}")
    with pytest.raises(SpecError, match="may only use"):
        built(train(), base=[BASE[0], bad, BASE[2]])


def test_two_relations_with_one_table_name_are_refused():
    other_id, other = source("events", {"time_column": "event_ts"}, EVENTS, schema="archive")
    sql = SQL + ' union all select * from "db"."archive"."events"'
    with pytest.raises(SpecError, match="both the table 'events'"):
        built(train(sql, (LABELS_ID, EVENTS_ID, other_id)), base=[*BASE, (other_id, other)])


def test_two_label_spines_are_ambiguous():
    other_id, other = source("labels2", SPINE, LABELS)
    sql = SQL + ' union all select * from "db"."raw"."labels2"'
    with pytest.raises(SpecError, match="more than one label_spine"):
        built(train(sql, (LABELS_ID, EVENTS_ID, other_id)), base=[*BASE, (other_id, other)])


def test_availability_without_a_time_column_is_refused():
    events = source("events", {"availability_column": "ingested_at"}, EVENTS)
    with pytest.raises(SpecError, match="need a time_column"):
        built(train(), base=[BASE[0], events, BASE[2]])


def test_tables_without_columns_are_a_gap_not_a_guess():
    bare = source("events", {"time_column": "event_ts", "availability_column": "ingested_at"})
    result = built(train(), base=[BASE[0], bare, BASE[2]])
    assert len(result.gaps) == 1
    assert "no columns are known for events (models/raw.yml)" in result.gaps[0]
    assert "--catalog-json" in result.gaps[0]


def test_untyped_time_columns_are_a_gap_and_catalog_json_fills_them():
    untyped = source(
        "events",
        {"time_column": "event_ts", "availability_column": "ingested_at"},
        columns(
            user_id="bigint", event_ts=None, ingested_at="timestamp", event_id=None, amount=None
        ),
    )
    result = built(train(), base=[BASE[0], untyped, BASE[2]])
    assert [g.split(" is unknown")[0] for g in result.gaps] == ["the type of events.event_ts"]
    typed = built(
        train(),
        base=[BASE[0], untyped, BASE[2]],
        column_types={EVENTS_ID: {"event_ts": "TIMESTAMP", "amount": "DOUBLE"}},
    )
    assert typed.gaps == [] and typed.schema["events"]["event_ts"] == "TIMESTAMP"
    assert typed.schema["events"]["event_id"] == "UNKNOWN"


def test_yaml_spec_wins_and_every_change_is_warned_about():
    override = {
        "boundary": "inclusive",
        "time_columns": {"events": "ingested_at", "other": "ts"},
        "static_tables": ["users"],
    }
    result = built(train(), override=override)
    assert result.spec.boundary == "inclusive"
    assert result.spec.time_columns == {"events": "ingested_at", "other": "ts"}
    assert result.spec.availability_columns == {"events": "ingested_at"}  # untouched fields stay
    assert result.warnings == [
        "--spec sets boundary = inclusive (the dbt meta does not); this can relax a rule",
        "--spec changes time_columns[events] from dbt meta: event_ts -> ingested_at",
        "--spec sets static_tables = users (the dbt meta does not); this can relax a rule",
    ]
    same = built(train(), override={"time_columns": {"EVENTS": "Event_TS"}, "boundary": "strict"})
    assert same.warnings == []


def test_merge_spec_replaces_lists_and_merges_mappings():
    merged, warnings = merge_spec(
        {"entity_keys": ["a"], "relations": {"x.y": "y"}},
        {"entity_keys": ["b"], "relations": {"x.z": "z"}},
    )
    assert merged == {"entity_keys": ["b"], "relations": {"x.y": "y", "x.z": "z"}}
    assert warnings == [
        "--spec changes entity_keys from dbt meta: ['a'] -> ['b']",
        "--spec sets relations[x.z] = z (the dbt meta does not); this can relax a rule",
    ]


def test_fixture_project_spec():
    found = parse_manifest(load_fixture())
    inlined = inline(found, found.get("model.jaffle_churn.training_churn_leaky"))
    derived = derive_spec(inlined, found.dialect)
    assert derived["label_table"] == "churn_labels"
    assert derived["time_columns"] == {"orders": "ordered_at"}
    assert derived["static_tables"] == ["customers"]
    assert derived["relations"]["jaffle.raw.orders"] == "raw.orders"


def test_every_spec_field_that_relaxes_a_rule_is_warned_about_even_when_meta_sets_nothing():
    override = {
        "boundary": "inclusive",
        "max_ingest_delay": {"events": "3 days"},
        "label_known_after": "14 days",
        "availability_columns": {"events": "ingested_at"},
        "static_tables": ["users"],
    }
    warned = built(train(), override=override).warnings
    assert [w.split(" = ")[0] for w in warned] == [
        "--spec sets boundary",
        "--spec sets max_ingest_delay[events]",
        "--spec sets label_known_after",
        "--spec sets static_tables",
    ]  # events already has this availability column in the meta: nothing changed there
    assert built(train(), override={"boundary": "strict"}).warnings == []
    assert built(train(), override={"boundary": "Strict"}).warnings == []


def test_duplicate_table_names_can_be_resolved_from_the_spec_file():
    other_id, other = source("events", {"time_column": "event_ts"}, EVENTS, schema="archive")
    sql = SQL + ' union all select * from "db"."archive"."events"'
    args = (train(sql, (LABELS_ID, EVENTS_ID, other_id)),)
    base = [*BASE, (other_id, other)]
    with pytest.raises(SpecError, match="iceberg_table.*relations"):
        built(*args, base=base)
    result = built(
        *args, base=base, override={"relations": {"db.archive.events": "archive.events_old"}}
    )
    assert set(result.spec.time_columns) == {"events", "events_old"}
    assert result.spec.relations["db.archive.events"] == "archive.events_old"
    assert {"events", "events_old"} <= set(result.schema)
