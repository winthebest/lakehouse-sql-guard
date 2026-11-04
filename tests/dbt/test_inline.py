import pytest
from sqlglot import exp

from sqlguard.dbt.inline import MAX_DEPTH, InlineError, inline
from sqlguard.dbt.manifest import parse_manifest
from tests.dbt.helpers import (
    BASE,
    EVENTS_ID,
    LABELS_ID,
    LEAKY_FEATURES,
    TRAINING,
    TRAINING_SQL,
    load_fixture,
    manifest,
    model,
)

STAGING = 'select user_id, event_ts, ingested_at, amount from "db"."raw"."events"'


def ctes(inlined):
    return [c.alias_or_name for c in inlined.tree.find_all(exp.CTE)]


def tables(inlined):
    return sorted(
        ".".join(p for p in (t.catalog, t.db, t.name) if p)
        for t in inlined.tree.find_all(exp.Table)
        if t.db
    )


def project(*extra):
    return parse_manifest(manifest(*BASE, *extra))


def test_two_tier_leak_inlines_both_layers_in_dependency_order():
    found = project(
        model("stg_events", STAGING, [EVENTS_ID]),
        model(
            "int_features",
            'select user_id, count(*) as n_events from "db"."main"."stg_events" group by user_id',
            ["model.proj.stg_events"],
        ),
        model("train", TRAINING_SQL, [LABELS_ID, "model.proj.int_features"], TRAINING),
    )
    inlined = inline(found, found.get("model.proj.train"))
    assert ctes(inlined) == ["dbt_model__stg_events", "dbt_model__int_features"]
    assert tables(inlined) == ["db.raw.events", "db.raw.labels"]  # sources only
    assert set(inlined.boundaries) == {EVENTS_ID, LABELS_ID}
    assert list(inlined.models) == [
        "model.proj.train",
        "model.proj.stg_events",
        "model.proj.int_features",
    ]
    # the upstream models are CTEs of one query that still parses and keeps the training select
    assert inlined.sql.startswith("WITH dbt_model__stg_events AS")
    assert "LEFT JOIN dbt_model__int_features AS f" in inlined.sql


def test_the_training_models_own_ctes_are_kept_after_the_inlined_ones():
    sql = (
        'with feats as (select * from "db"."main"."int_features") '
        'select l.user_id, l.label_ts, l.churned, feats.n_events from "db"."raw"."labels" l '
        "left join feats on feats.user_id = l.user_id"
    )
    found = project(
        model("int_features", LEAKY_FEATURES, [EVENTS_ID]),
        model("train", sql, [LABELS_ID, "model.proj.int_features"], TRAINING),
    )
    assert ctes(inline(found, found.get("model.proj.train"))) == [
        "dbt_model__int_features",
        "feats",
    ]


def test_an_unaliased_reference_keeps_its_name_for_qualified_columns():
    sql = (
        "select labels.user_id, labels.label_ts, labels.churned, int_features.n_events "
        'from "db"."raw"."labels" as labels join "db"."main"."int_features" '
        "on int_features.user_id = labels.user_id"
    )
    found = project(
        model("int_features", LEAKY_FEATURES, [EVENTS_ID]),
        model("train", sql, [LABELS_ID, "model.proj.int_features"], TRAINING),
    )
    assert (
        "dbt_model__int_features AS int_features"
        in inline(found, found.get("model.proj.train")).sql
    )


def test_a_model_used_twice_becomes_one_cte():
    sql = (
        "select l.user_id, l.label_ts, l.churned, a.n_events, b.n_events as n2 "
        'from "db"."raw"."labels" l '
        'left join "db"."main"."int_features" a on a.user_id = l.user_id '
        'left join "db"."main"."int_features" b on b.user_id = l.user_id'
    )
    found = project(
        model("int_features", LEAKY_FEATURES, [EVENTS_ID]),
        model("train", sql, [LABELS_ID, "model.proj.int_features"], TRAINING),
    )
    assert ctes(inline(found, found.get("model.proj.train"))) == ["dbt_model__int_features"]


@pytest.mark.parametrize(
    "leakage",
    [
        {"role": "static"},
        {"role": "label_spine", "prediction_ts": "label_ts"},
        {"time_column": "event_ts"},
    ],
    ids=["static", "label_spine", "declared_temporal"],
)
def test_models_declared_as_spine_static_or_temporal_stay_physical_tables(leakage):
    found = project(
        model("dim_events", STAGING, [EVENTS_ID], leakage),
        model(
            "train",
            'select * from "db"."main"."dim_events"',
            ["model.proj.dim_events"],
            TRAINING,
        ),
    )
    inlined = inline(found, found.get("model.proj.train"))
    assert ctes(inlined) == []
    assert tables(inlined) == ["db.main.dim_events"]
    assert set(inlined.boundaries) == {"model.proj.dim_events"}


def test_seeds_and_snapshots_stop_the_walk():
    data = manifest(
        *BASE, model("train", 'select * from "db"."main"."countries"', ["seed.proj.c"], TRAINING)
    )
    data["nodes"]["seed.proj.c"] = {
        "resource_type": "seed",
        "name": "countries",
        "relation_name": '"db"."main"."countries"',
        "compiled_code": "this is not sql and must not be read",
    }
    found = parse_manifest(data)
    inlined = inline(found, found.get("model.proj.train"))
    assert tables(inlined) == ["db.main.countries"]


def test_ephemeral_models_are_left_to_dbt():
    sql = (
        'with __dbt__cte__eph as (select user_id from "db"."raw"."events") '
        "select * from __dbt__cte__eph"
    )
    found = project(
        model("eph", None, [EVENTS_ID], materialized="ephemeral"),
        model("train", sql, ["model.proj.eph"], TRAINING),
    )
    inlined = inline(found, found.get("model.proj.train"))
    assert ctes(inlined) == ["__dbt__cte__eph"] and tables(inlined) == ["db.raw.events"]


def test_a_table_no_dbt_node_provides_stays_as_written():
    found = project(model("train", 'select * from "db"."ext"."mystery"', [], TRAINING))
    assert tables(inline(found, found.get("model.proj.train"))) == ["db.ext.mystery"]


def test_missing_compiled_code_says_how_to_compile():
    found = project(
        model("int_features", None, [EVENTS_ID]),
        model("train", TRAINING_SQL, [LABELS_ID, "model.proj.int_features"], TRAINING),
    )
    with pytest.raises(
        InlineError,
        match=r"model\.proj\.int_features has no compiled_code.*"
        r"--select \+train",
    ):
        inline(found, found.get("model.proj.train"))
    missing_root = project(model("train", None, [], TRAINING))
    with pytest.raises(InlineError, match="--select \\+train"):
        inline(missing_root, missing_root.get("model.proj.train"))


def test_cycles_are_detected():
    found = project(
        model("a", 'select * from "db"."main"."b"', ["model.proj.b"]),
        model("b", 'select * from "db"."main"."a"', ["model.proj.a"]),
        model("train", 'select * from "db"."main"."a"', ["model.proj.a"], TRAINING),
    )
    with pytest.raises(InlineError, match="cycle: a -> b -> a"):
        inline(found, found.get("model.proj.train"))


def test_depth_is_limited():
    chain = [model("m0", 'select * from "db"."raw"."events"', [EVENTS_ID])]
    for i in range(1, 6):
        chain.append(
            model(f"m{i}", f'select * from "db"."main"."m{i - 1}"', [f"model.proj.m{i - 1}"])
        )
    chain.append(model("train", 'select * from "db"."main"."m5"', ["model.proj.m5"], TRAINING))
    found = project(*chain)
    train = found.get("model.proj.train")
    assert len(ctes(inline(found, train))) == 6
    with pytest.raises(InlineError, match="more than 3 nested models"):
        inline(found, train, max_depth=3)
    assert MAX_DEPTH >= 32


def test_unparseable_and_non_select_sql_are_errors():
    for sql, message in [("select from where", "cannot parse"), ("drop table x", "not a SELECT")]:
        found = project(model("train", sql, [], TRAINING))
        with pytest.raises(InlineError, match=message):
            inline(found, found.get("model.proj.train"))


def test_a_cte_named_like_an_inlined_one_is_refused():
    found = project(
        model("train", "with dbt_model__x as (select 1) select * from dbt_model__x", [], TRAINING)
    )
    with pytest.raises(InlineError, match="reserved"):
        inline(found, found.get("model.proj.train"))


def test_two_models_with_one_name_get_distinct_ctes():
    data = manifest(
        *BASE, model("train", TRAINING_SQL, [LABELS_ID, "model.proj.int_features"], TRAINING)
    )
    _, other = model("int_features", LEAKY_FEATURES, [EVENTS_ID])
    data["nodes"]["model.other.int_features"] = {**other, "unique_id": "model.other.int_features"}
    data["nodes"]["model.proj.int_features"] = {
        **other,
        "relation_name": '"db"."main"."int_features"',
    }
    data["nodes"]["model.other.int_features"]["relation_name"] = '"db"."alt"."int_features"'
    data["nodes"]["model.proj.train"]["depends_on"]["nodes"].append("model.other.int_features")
    data["nodes"]["model.proj.train"]["compiled_code"] += (
        ' left join "db"."alt"."int_features" g on g.user_id = l.user_id'
    )
    found = parse_manifest(data)
    assert ctes(inline(found, found.get("model.proj.train"))) == [
        "dbt_model__int_features",
        "dbt_model__int_features_2",
    ]


def test_nodes_are_tagged_with_the_model_their_sql_came_from():
    found = parse_manifest(load_fixture())
    inlined = inline(found, found.get("model.jaffle_churn.training_churn_leaky"))
    owners = {t.name: t.meta["dbt_node"] for t in inlined.tree.find_all(exp.Table) if t.db}
    assert owners == {
        "orders": "model.jaffle_churn.stg_orders",
        "customers": "model.jaffle_churn.stg_customers",
        "churn_labels": "model.jaffle_churn.training_churn_leaky",
    }
    assert set(inlined.queries) == set(inlined.models)


def test_the_real_example_inlines_to_sources_only():
    found = parse_manifest(load_fixture())
    inlined = inline(found, found.get("model.jaffle_churn.training_churn_clean"))
    assert tables(inlined) == [
        "jaffle.raw.churn_labels",
        "jaffle.raw.churn_labels",
        "jaffle.raw.customers",
        "jaffle.raw.orders",
    ]
