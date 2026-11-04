import json

import pytest

from sqlguard.cli import main
from sqlguard.dbt.manifest import (
    ManifestError,
    load_column_types,
    load_manifest,
    parse_manifest,
)
from tests.dbt.helpers import FIXTURE, load_fixture, manifest, model, source, write


def exit_code(argv):
    with pytest.raises(SystemExit) as info:
        main(argv)
    return info.value.code


def test_reads_the_real_manifest_fixture():
    found = load_manifest(FIXTURE)
    assert found.adapter == "duckdb" and found.dialect == "duckdb"
    leaky = found.get("model.jaffle_churn.training_churn_leaky")
    assert leaky.kind == "model" and leaky.role == "training_set"
    assert leaky.depends_on[1] == "model.jaffle_churn.int_customer_features"
    assert leaky.relation_name == '"jaffle"."main"."training_churn_leaky"'
    assert (
        leaky.compiled_code and leaky.original_file_path == "models/marts/training_churn_leaky.sql"
    )
    orders = found.get("source.jaffle_churn.raw.orders")
    assert orders.kind == "source"
    assert orders.leakage["time_column"] == "ordered_at"
    assert orders.columns["ordered_at"].data_type == "timestamp"
    assert [n.name for n in found.with_role("training_set")] == [
        "training_churn_clean",
        "training_churn_leaky",
    ]


@pytest.mark.parametrize(
    "version",
    [
        "https://schemas.getdbt.com/dbt/manifest/v11.json",
        "https://schemas.getdbt.com/dbt/manifest/v13.json",
        "https://schemas.getdbt.com/dbt/manifest/v1.json",
        "",
    ],
)
def test_other_schema_versions_are_refused(version):
    data = load_fixture()
    data["metadata"]["dbt_schema_version"] = version
    with pytest.raises(ManifestError, match="only manifest v12"):
        parse_manifest(data)


@pytest.mark.parametrize("version", ["v11", "v13"])
def test_cli_exits_two_for_a_foreign_manifest(tmp_path, capsys, version):
    data = load_fixture()
    data["metadata"]["dbt_schema_version"] = (
        f"https://schemas.getdbt.com/dbt/manifest/{version}.json"
    )
    path = write(tmp_path / "manifest.json", data)
    assert exit_code(["leakage", "dbt", "--manifest", str(path)]) == 2
    assert f"manifest/{version}.json" in capsys.readouterr().err


def test_cli_exits_two_for_unreadable_input(tmp_path, capsys):
    broken = tmp_path / "manifest.json"
    broken.write_text("{not json")
    assert exit_code(["leakage", "dbt", "--manifest", str(broken)]) == 2
    assert "not valid JSON" in capsys.readouterr().err
    assert exit_code(["leakage", "dbt", "--manifest", str(tmp_path / "missing.json")]) == 2
    broken.write_text("[]")
    assert exit_code(["leakage", "dbt", "--manifest", str(broken)]) == 2


def test_meta_comes_from_config_and_falls_back_to_the_node():
    from_config = source("a")[1]
    from_config["config"]["meta"] = {"leakage": {"role": "static"}}
    from_config["meta"] = {}
    from_node = source("b")[1]
    from_node["config"] = {}
    from_node["meta"] = {"leakage": {"role": "static"}}
    both = source("c")[1]
    both["config"]["meta"] = {"leakage": {"role": "static"}}
    both["meta"] = {"leakage": {"role": "label_spine"}}
    found = parse_manifest(
        manifest(
            ("source.proj.raw.a", from_config),
            ("source.proj.raw.b", from_node),
            ("source.proj.raw.c", both),
        )
    )
    assert [found.get(f"source.proj.raw.{n}").role for n in "abc"] == ["static", "static", "static"]


def test_column_meta_is_read_from_config_with_a_fallback():
    node = source(
        "a", cols={"x": {"name": "x", "meta": {"k": 1}}, "y": {"config": {"meta": {"k": 2}}}}
    )[1]
    columns = parse_manifest(manifest(("source.proj.raw.a", node))).get("source.proj.raw.a").columns
    assert columns["x"].meta == {"k": 1} and columns["y"].meta == {"k": 2}
    assert columns["y"].name == "y" and columns["y"].data_type is None


def test_unknown_role_is_an_error_not_an_unchecked_model():
    data = manifest(model("m", "select 1", leakage={"role": "trainng_set"}))
    with pytest.raises(ManifestError, match="trainng_set"):
        parse_manifest(data).with_role("training_set")


def test_leakage_meta_must_be_a_mapping():
    data = manifest(model("m", "select 1"))
    data["nodes"]["model.proj.m"]["config"]["meta"] = {"leakage": "yes"}
    with pytest.raises(ManifestError, match="must be a mapping"):
        parse_manifest(data).with_role("training_set")


def test_unknown_adapter_has_no_dialect():
    with pytest.raises(ManifestError, match="no matching SQL dialect"):
        parse_manifest(manifest(model("m", "select 1"), adapter="notadb"))
    assert parse_manifest(manifest(model("m", "select 1"), adapter="sqlserver")).dialect == "tsql"


def test_only_models_seeds_snapshots_and_sources_are_nodes():
    data = manifest(model("m", "select 1"))
    data["nodes"]["test.proj.t"] = {"resource_type": "test", "name": "t"}
    assert set(parse_manifest(data).nodes) == {"model.proj.m"}


def test_catalog_json_gives_lowercase_column_types(tmp_path):
    path = tmp_path / "catalog.json"
    path.write_text(
        json.dumps(
            {
                "nodes": {"model.p.a": {"columns": {"Id": {"type": "BIGINT"}}}},
                "sources": {"source.p.r.b": {"columns": {"TS": {"type": "TIMESTAMP"}}}},
            }
        )
    )
    assert load_column_types(path) == {
        "model.p.a": {"id": "BIGINT"},
        "source.p.r.b": {"ts": "TIMESTAMP"},
    }
