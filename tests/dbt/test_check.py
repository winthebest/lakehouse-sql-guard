import json

import pytest
import yaml

from sqlguard.cli import main
from sqlguard.dbt.check import check_model, training_sets
from sqlguard.dbt.manifest import ManifestError, parse_manifest
from tests.dbt.helpers import (
    BASE,
    EVENTS_ID,
    LABELS_ID,
    TRAINING,
    TRAINING_SQL,
    USERS_ID,
    columns,
    load_fixture,
    manifest,
    model,
    source,
    write,
)

LEAKY = "model.jaffle_churn.training_churn_leaky"
CLEAN = "model.jaffle_churn.training_churn_clean"
INT_FEATURES = "models/intermediate/int_customer_features.sql"


def exit_code(argv):
    with pytest.raises(SystemExit) as info:
        main(argv)
    return info.value.code


def check(data, unique_id, **kwargs):
    found = parse_manifest(data)
    return check_model(found, found.get(unique_id), **kwargs)


def files_of(models):
    return [m["file"] for m in models]


def test_the_leak_in_an_intermediate_model_is_found_and_attributed_to_it():
    result = check(load_fixture(), LEAKY)
    assert result.report.verdict == "leaky" and result.report.leak_types == ["L2"]
    (finding,) = result.report.findings
    assert files_of(result.to_dict()["findings"][0]["models"]) == [INT_FEATURES]
    assert finding.location == "orders"


def test_the_clean_training_model_is_clean():
    result = check(load_fixture(), CLEAN)
    assert result.report.verdict == "clean" and result.report.findings == []


def test_a_meta_only_change_is_checked_again():
    data = load_fixture()
    assert check(data, CLEAN).report.verdict == "clean"
    # the SQL of every model is unchanged; only what the orders source declares about itself is
    orders = data["sources"]["source.jaffle_churn.raw.orders"]
    for meta in (orders["config"]["meta"], orders["meta"]):
        meta["leakage"]["time_column"] = "order_ts"  # renamed in the warehouse, not here
    changed = check(data, CLEAN)
    assert changed.report.verdict == "unverifiable"
    assert [f.rule_id for f in changed.report.findings] == ["SPEC"]
    assert "orders.order_ts not in schema" in changed.report.findings[0].message
    # and the training sets are picked up from the manifest alone, with no state to compare to
    data["nodes"][LEAKY]["config"]["meta"] = data["nodes"][LEAKY]["meta"] = {}
    assert [n.name for n in training_sets(parse_manifest(data))] == ["training_churn_clean"]


def test_every_training_set_is_checked_unless_selected():
    found = parse_manifest(load_fixture())
    assert [n.name for n in training_sets(found)] == [
        "training_churn_clean",
        "training_churn_leaky",
    ]
    assert [n.name for n in training_sets(found, ["*leaky"])] == ["training_churn_leaky"]
    assert [n.name for n in training_sets(found, ["models/marts/training_churn_c*.sql"])] == [
        "training_churn_clean"
    ]
    assert [n.name for n in training_sets(found, [CLEAN, "nothing"])] == ["training_churn_clean"]
    with pytest.raises(ManifestError, match="matches no training_set"):
        training_sets(found, ["stg_orders"])
    with pytest.raises(ManifestError, match="no model declares"):
        training_sets(parse_manifest(manifest(model("m", "select 1"))))


def test_a_leak_in_the_middle_of_three_layers_is_attributed_to_the_aggregating_model():
    stg = 'select user_id, event_ts, ingested_at from "db"."raw"."events"'
    renamed = 'select user_id as user_id, event_ts from "db"."main"."stg_events"'
    agg = 'select user_id, count(*) as n_events from "db"."main"."int_renamed" group by user_id'
    data = manifest(
        *BASE,
        model("stg_events", stg, [EVENTS_ID]),
        model("int_renamed", renamed, ["model.proj.stg_events"]),
        model("int_features", agg, ["model.proj.int_renamed"]),
        model("train", TRAINING_SQL, [LABELS_ID, "model.proj.int_features"], TRAINING),
    )
    result = check(data, "model.proj.train")
    assert result.report.verdict == "leaky"
    leak = next(f for f in result.to_dict()["findings"] if f["rule_id"] == "L2")
    assert files_of(leak["models"]) == ["models/int_features.sql"]


def test_a_leak_written_in_the_training_model_is_attributed_to_it():
    sql = (
        'select l.user_id, l.label_ts, l.churned, count(e.event_id) n from "db"."raw"."labels" l '
        'left join "db"."raw"."events" e on e.user_id = l.user_id group by 1, 2, 3'
    )
    data = manifest(*BASE, model("train", sql, [LABELS_ID, EVENTS_ID], TRAINING))
    result = check(data, "model.proj.train")
    assert result.report.verdict == "leaky"
    assert {m.unique_id for a in result.finding_models for m in a.models} == {"model.proj.train"}


def test_a_clock_call_in_an_intermediate_model_is_attributed_to_it():
    stg = 'select user_id, event_ts from "db"."raw"."events"'
    clocked = (
        "select user_id, date_diff('day', max(event_ts), current_date) as idle_days "
        'from "db"."main"."stg_events" group by user_id'
    )
    sql = TRAINING_SQL.replace("n_events", "idle_days")
    data = manifest(
        *BASE,
        model("stg_events", stg, [EVENTS_ID]),
        model("int_features", clocked, ["model.proj.stg_events"]),
        model("train", sql, [LABELS_ID, "model.proj.int_features"], TRAINING),
    )
    findings = check(data, "model.proj.train").to_dict()["findings"]
    by_rule = {f["rule_id"]: files_of(f["models"]) for f in findings}
    assert by_rule["L7"] == ["models/int_features.sql"]


def test_a_missing_column_list_makes_the_model_unverifiable():
    data = load_fixture()
    data["sources"]["source.jaffle_churn.raw.orders"]["columns"] = {}
    result = check(data, CLEAN)
    assert result.report.verdict == "unverifiable"
    assert [f.rule_id for f in result.report.findings] == ["SPEC"]
    assert "no columns are known for orders" in result.report.findings[0].message


def test_suggested_fixes_point_at_the_model_with_the_leak(tmp_path):
    path = write(tmp_path / "manifest.json", load_fixture())
    before = path.read_bytes()
    result = check(load_fixture(), LEAKY, suggest=True)
    (fix,) = result.fixes
    assert fix.rule == "L2" and not fix.rewrites  # an aggregate by entity alone is only explained
    assert result.fix_files == [INT_FEATURES]
    assert result.diff_file == "models/marts/training_churn_leaky.sql"  # nothing to diff
    assert files_of(result.to_dict()["fixes"][0]["models"]) == [INT_FEATURES]
    assert path.read_bytes() == before and list(tmp_path.iterdir()) == [path]


def test_a_rewritable_leak_in_an_intermediate_model_is_diffed_against_its_file():
    # the model that joins the spine to the events reads them without a bound
    joined = (
        'select l.user_id, l.label_ts, count(e.event_id) as n_events from "db"."raw"."labels" l '
        'left join "db"."raw"."events" e on e.user_id = l.user_id '
        "group by l.user_id, l.label_ts"
    )
    sql = (
        'select l.user_id, l.label_ts, l.churned, f.n_events from "db"."raw"."labels" l '
        'left join "db"."main"."int_features" f on f.user_id = l.user_id '
        "and f.label_ts = l.label_ts"
    )
    data = manifest(
        *BASE,
        model("int_features", joined, [LABELS_ID, EVENTS_ID]),
        model("train", sql, [LABELS_ID, "model.proj.int_features"], TRAINING),
    )
    result = check(data, "model.proj.train", suggest=True)
    assert result.report.verdict == "leaky"
    rewrites = [f for f in result.fixes if f.rewrites]
    assert rewrites and "ingested_at" in rewrites[-1].after_sql
    assert result.diff_file == "models/int_features.sql"


def test_json_has_the_fields_of_leakage_check(tmp_path, capsys):
    from tests.cli.test_cli import LEAKY as CHECK_LEAKY
    from tests.leakage.conftest import SCHEMA, SPEC

    for name, content in (("spec.yaml", SPEC), ("schema.yaml", SCHEMA)):
        (tmp_path / name).write_text(yaml.safe_dump(content))
    (tmp_path / "q.sql").write_text(CHECK_LEAKY)
    assert exit_code(
        ["leakage", "check", str(tmp_path / "q.sql"), "--spec", str(tmp_path / "spec.yaml"),
         "--schema", str(tmp_path / "schema.yaml"), "--json", "--suggest-fix"]
    ) == 1  # fmt: skip
    (reference,) = json.loads(capsys.readouterr().out)

    manifest_path = write(tmp_path / "manifest.json", load_fixture())
    assert exit_code(["leakage", "dbt", "--manifest", str(manifest_path), "--json",
                      "--suggest-fix", "--select", "*leaky"]) == 1  # fmt: skip
    (entry,) = json.loads(capsys.readouterr().out)
    assert set(reference) <= set(entry)
    assert set(reference["findings"][0]) <= set(entry["findings"][0])
    assert set(reference["fixes"][0]) <= set(entry["fixes"][0])
    assert entry["file"] == "models/marts/training_churn_leaky.sql" and entry["node"] == LEAKY
    assert entry["findings"][0]["models"] == [
        {"node": "model.jaffle_churn.int_customer_features", "file": INT_FEATURES}
    ]


def test_cli_exit_codes_and_text_output(tmp_path, capsys):
    path = str(write(tmp_path / "manifest.json", load_fixture()))
    assert exit_code(["leakage", "dbt", "--manifest", path, "--select", "*clean"]) == 0
    assert capsys.readouterr().out.startswith("CLEAN")
    assert exit_code(["leakage", "dbt", "--manifest", path]) == 1
    out = capsys.readouterr().out
    assert out.index("CLEAN") < out.index("LEAKY")
    assert f"in {INT_FEATURES}" in out
    assert exit_code(["leakage", "dbt", "--manifest", path, "--select", "nothing"]) == 2
    assert exit_code(["leakage", "dbt", "--manifest", path, "--replay"]) == 2
    assert "--replay needs" in capsys.readouterr().err


def test_unverifiable_fails_closed_unless_allowed(tmp_path):
    data = load_fixture()
    data["sources"]["source.jaffle_churn.raw.orders"]["columns"] = {}
    path = str(write(tmp_path / "manifest.json", data))
    argv = ["leakage", "dbt", "--manifest", path, "--select", "*clean"]
    assert exit_code(argv) == 1
    assert exit_code([*argv, "--allow-unverifiable"]) == 0


def test_a_manifest_from_dbt_parse_names_the_compile_command(tmp_path, capsys):
    data = load_fixture()
    for node in data["nodes"].values():
        node["compiled_code"] = None
    path = str(write(tmp_path / "manifest.json", data))
    assert exit_code(["leakage", "dbt", "--manifest", path]) == 2
    assert "dbt compile --select +" in capsys.readouterr().err


def test_spec_file_overrides_dbt_meta_with_a_warning(tmp_path, capsys):
    path = str(write(tmp_path / "manifest.json", load_fixture()))
    spec = tmp_path / "leakage.yml"
    spec.write_text(yaml.safe_dump({"boundary": "inclusive", "dialect": "duckdb"}))
    argv = ["leakage", "dbt", "--manifest", path, "--select", "*clean", "--spec", str(spec)]
    assert exit_code(argv) == 0
    spec.write_text(yaml.safe_dump({"time_columns": {"orders": "loaded_at"}}))
    assert exit_code(argv) == 1  # the file wins: with that time column the table is only a hint
    assert "--spec changes time_columns[orders] from dbt meta: ordered_at -> loaded_at" in (
        capsys.readouterr().err
    )
    spec.write_text("- not a mapping")
    assert exit_code(argv) == 2


def test_catalog_json_supplies_types(tmp_path):
    data = load_fixture()
    orders = data["sources"]["source.jaffle_churn.raw.orders"]
    orders["columns"]["ordered_at"]["data_type"] = None
    manifest_path = str(write(tmp_path / "manifest.json", data))
    argv = ["leakage", "dbt", "--manifest", manifest_path, "--select", "*clean"]
    assert exit_code(argv) == 1  # the type of a time column is not guessed
    catalog = tmp_path / "catalog.json"
    catalog.write_text(
        json.dumps(
            {
                "sources": {
                    "source.jaffle_churn.raw.orders": {
                        "columns": {"ordered_at": {"type": "TIMESTAMP"}}
                    }
                }
            }
        )
    )
    assert exit_code([*argv, "--catalog-json", str(catalog)]) == 0


# --- ephemeral models: dbt pastes their SQL into the parent, their reads stay on their node ---

EPH_TRAIN = (
    'select l.user_id, l.label_ts, l.churned, f.n_events from "db"."raw"."labels" l '
    "left join __dbt__cte__int_features f on f.user_id = l.user_id"
)
EPH_AGG = 'select user_id, count(*) as n_events from "db"."raw"."events" group by user_id'


def paste(*ctes: tuple[str, str], then: str) -> str:
    """The compiled SQL of a model that depends on ephemeral models, as dbt writes it."""
    return "with " + ", ".join(f"__dbt__cte__{n} as ({sql})" for n, sql in ctes) + " " + then


def test_a_leak_in_an_ephemeral_intermediate_model_is_found_and_attributed_to_it(tmp_path):
    data = manifest(
        *BASE,
        model("int_features", None, [EVENTS_ID], materialized="ephemeral"),
        model(
            "train",
            paste(("int_features", EPH_AGG), then=EPH_TRAIN),
            [LABELS_ID, "model.proj.int_features"],
            TRAINING,
        ),
    )
    result = check(data, "model.proj.train")
    assert result.report.verdict == "leaky"
    leak = next(f for f in result.to_dict()["findings"] if f["rule_id"] == "L3")
    assert files_of(leak["models"]) == ["models/int_features.sql"]
    path = str(write(tmp_path / "manifest.json", data))
    assert exit_code(["leakage", "dbt", "--manifest", path]) == 1


def test_an_ephemeral_model_can_read_another_source_than_its_parent():
    orders_id, orders = source(
        "orders",
        {"time_column": "order_ts"},
        columns(user_id="bigint", order_ts="timestamp", amount="double"),
    )
    agg = 'select user_id, sum(amount) as n_events from "db"."raw"."orders" group by user_id'
    data = manifest(
        *BASE,
        (orders_id, orders),
        model("int_features", None, [orders_id], materialized="ephemeral"),
        model(
            "train",
            paste(("int_features", agg), then=EPH_TRAIN),
            [LABELS_ID, "model.proj.int_features"],
            TRAINING,
        ),
    )
    result = check(data, "model.proj.train")
    assert result.report.verdict == "leaky"
    assert {f.rule_id for f in result.report.findings} <= {"L2", "L3"}
    assert "PARSE" not in {f.rule_id for f in result.report.findings}


def test_an_ephemeral_model_reading_a_view_model_has_that_model_inlined():
    stg = 'select user_id, event_ts from "db"."raw"."events"'
    agg = 'select user_id, count(*) as n_events from "db"."main"."stg_events" group by user_id'
    data = manifest(
        *BASE,
        model("stg_events", stg, [EVENTS_ID]),
        model("int_features", None, ["model.proj.stg_events"], materialized="ephemeral"),
        model(
            "train",
            paste(("int_features", agg), then=EPH_TRAIN),
            [LABELS_ID, "model.proj.int_features"],
            TRAINING,
        ),
    )
    result = check(data, "model.proj.train")
    assert result.report.verdict == "leaky"
    assert "dbt_model__stg_events" in result.sql
    leak = next(f for f in result.to_dict()["findings"] if f["rule_id"] == "L2")
    assert files_of(leak["models"]) == ["models/int_features.sql"]


def test_a_chain_of_two_ephemeral_models_is_followed_to_the_source(tmp_path):
    first = 'select user_id, event_ts from "db"."raw"."events"'
    second = "select user_id, count(*) as n_events from __dbt__cte__first group by user_id"
    sql = (
        'select l.user_id, l.label_ts, l.churned, f.n_events from "db"."raw"."labels" l '
        "left join __dbt__cte__second f on f.user_id = l.user_id"
    )
    data = manifest(
        *BASE,
        model("first", None, [EVENTS_ID], materialized="ephemeral"),
        model("second", None, ["model.proj.first"], materialized="ephemeral"),
        model(
            "train",
            paste(("first", first), ("second", second), then=sql),
            [LABELS_ID, "model.proj.second"],
            TRAINING,
        ),
    )
    result = check(data, "model.proj.train")
    assert result.report.verdict == "leaky"
    leak = next(f for f in result.to_dict()["findings"] if f["rule_id"] == "L2")
    assert files_of(leak["models"]) == ["models/second.sql"]
    path = str(write(tmp_path / "manifest.json", data))
    assert exit_code(["leakage", "dbt", "--manifest", path]) == 1


def test_a_bounded_ephemeral_model_is_clean():
    bounded = (
        'select l.user_id, l.label_ts, count(e.event_id) as n_events from "db"."raw"."labels" l '
        'left join "db"."raw"."events" e on e.user_id = l.user_id and e.event_ts < l.label_ts '
        "and e.ingested_at < l.label_ts group by l.user_id, l.label_ts"
    )
    sql = (
        'select l.user_id, l.label_ts, l.churned, f.n_events from "db"."raw"."labels" l '
        "left join __dbt__cte__int_features f on f.user_id = l.user_id and f.label_ts = l.label_ts"
    )
    data = manifest(
        *BASE,
        model("int_features", None, [LABELS_ID, EVENTS_ID], materialized="ephemeral"),
        model(
            "train",
            paste(("int_features", bounded), then=sql),
            [LABELS_ID, "model.proj.int_features"],
            TRAINING,
        ),
    )
    assert check(data, "model.proj.train").report.verdict == "clean"


# --- attribution: the model that introduces the unbounded read, not a thin staging model ---


def test_a_filtering_or_static_joining_staging_model_is_not_blamed():
    stg = (
        "select e.event_id, e.user_id, e.event_ts, e.ingested_at, e.amount "
        'from "db"."raw"."events" e join "db"."raw"."users" u on u.user_id = e.user_id '
        "where e.amount > 0"
    )
    data = manifest(
        *BASE,
        model("stg_events", stg, [EVENTS_ID, USERS_ID]),
        model(
            "int_features",
            'select user_id, count(*) as n_events from "db"."main"."stg_events" group by user_id',
            ["model.proj.stg_events"],
        ),
        model("train", TRAINING_SQL, [LABELS_ID, "model.proj.int_features"], TRAINING),
    )
    result = check(data, "model.proj.train", suggest=True)
    leak = next(f for f in result.to_dict()["findings"] if f["rule_id"] == "L2")
    assert (
        files_of(leak["models"]) == ["models/int_features.sql"] and leak["attribution"] == "model"
    )
    assert result.fix_files == ["models/int_features.sql"]


def test_a_table_reaching_the_training_set_along_two_paths_lists_candidates():
    stg = 'select user_id, event_ts, ingested_at from "db"."raw"."events"'
    data = manifest(
        *BASE,
        model("stg_events", stg, [EVENTS_ID]),
        model(
            "int_a",
            'select user_id, count(*) as a from "db"."main"."stg_events" group by user_id',
            ["model.proj.stg_events"],
        ),
        model(
            "int_b",
            'select user_id, max(event_ts) as b from "db"."main"."stg_events" group by user_id',
            ["model.proj.stg_events"],
        ),
        model(
            "train",
            'select l.user_id, l.label_ts, l.churned, a.a, b.b from "db"."raw"."labels" l '
            'left join "db"."main"."int_a" a on a.user_id = l.user_id '
            'left join "db"."main"."int_b" b on b.user_id = l.user_id',
            [LABELS_ID, "model.proj.int_a", "model.proj.int_b"],
            TRAINING,
        ),
    )
    result = check(data, "model.proj.train")
    leak = next(f for f in result.to_dict()["findings"] if f["rule_id"] == "L2")
    assert sorted(files_of(leak["models"])) == ["models/int_a.sql", "models/int_b.sql"]
    assert leak["attribution"] == "candidates"
    assert all("one of: " in f for f in result.fix_files)


# --- models read as declared are reported, not silently trusted ---


def trusted_project():
    stg = 'select user_id, event_ts, ingested_at, amount from "db"."raw"."events"'
    sql = (
        'select l.user_id, l.label_ts, l.churned, f.amount from "db"."raw"."labels" l '
        'left join "db"."main"."dim_events" f on f.user_id = l.user_id '
        "and f.event_ts < l.label_ts and f.ingested_at < l.label_ts"
    )
    return manifest(
        *BASE,
        model(
            "dim_events",
            stg,
            [EVENTS_ID],
            {"time_column": "event_ts", "availability_column": "ingested_at"},
            columns(
                user_id="bigint", event_ts="timestamp", ingested_at="timestamp", amount="double"
            ),
        ),
        model("train", sql, [LABELS_ID, "model.proj.dim_events"], TRAINING),
    )


def test_models_used_as_stop_points_are_listed_in_json_and_text(tmp_path, capsys):
    result = check(trusted_project(), "model.proj.train")
    assert result.report.verdict == "clean"
    assert result.to_dict()["trusted_models"] == [
        {
            "node": "model.proj.dim_events",
            "file": "models/dim_events.sql",
            "declared": "time_column",
        }
    ]
    path = str(write(tmp_path / "manifest.json", trusted_project()))
    assert exit_code(["leakage", "dbt", "--manifest", path]) == 0
    out = capsys.readouterr().out
    assert "trusted without inlining (their SQL is not checked): dim_events" in out
    # a project with no such model reports an empty list
    assert check(load_fixture(), CLEAN).to_dict()["trusted_models"] == []


def test_a_model_file_edited_after_the_manifest_is_warned_about(tmp_path, capsys, monkeypatch):
    import os

    monkeypatch.chdir(
        tmp_path
    )  # a file path is looked up from here and from the manifest's project
    project = tmp_path / "proj"
    (project / "build_ci").mkdir(parents=True)
    path = write(project / "build_ci" / "manifest.json", load_fixture())
    edited = project / "models" / "intermediate" / "int_customer_recent_orders.sql"
    edited.parent.mkdir(parents=True)
    edited.write_text("select 1")
    os.utime(path, (1_000_000, 1_000_000))
    argv = ["leakage", "dbt", "--manifest", str(path), "--select", "*clean"]
    assert exit_code(argv) == 0  # a warning, not a failure
    err = capsys.readouterr().err
    assert "models/intermediate/int_customer_recent_orders.sql changed after" in err
    assert "run `dbt compile` first" in err
    os.utime(edited, (500_000, 500_000))
    assert exit_code(argv) == 0
    assert "changed after" not in capsys.readouterr().err
