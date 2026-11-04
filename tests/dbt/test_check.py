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
    load_fixture,
    manifest,
    model,
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
    assert {m.unique_id for ms in result.finding_models for m in ms} == {"model.proj.train"}


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
