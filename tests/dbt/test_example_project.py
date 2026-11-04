"""The example project end to end: real `dbt compile` on DuckDB, an Iceberg warehouse with weekly
commits, then the guard on the manifest dbt wrote. Skipped when the `dbt` extra is not installed."""

import json
import os
import runpy
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("dbt.adapters.duckdb")

from sqlguard.cli import main  # noqa: E402

EXAMPLE = Path(__file__).parents[2] / "examples" / "dbt"
LEAKY_FILE = "models/marts/training_churn_leaky.sql"
CLEAN_FILE = "models/marts/training_churn_clean.sql"
INT_FILE = "models/intermediate/int_customer_features.sql"


def exit_code(argv):
    with pytest.raises(SystemExit) as info:
        main(argv)
    return info.value.code


@pytest.fixture(scope="module")
def project(tmp_path_factory):
    root = tmp_path_factory.mktemp("jaffle")
    shutil.copytree(
        EXAMPLE, root / "dbt", ignore=shutil.ignore_patterns("build*", "logs", "*.duckdb")
    )
    warehouse = root / "warehouse"
    runpy.run_path(str(EXAMPLE / "make-warehouse.py"))["build"](warehouse)
    env = {
        **os.environ,
        "ICEBERG_WAREHOUSE": str(warehouse),
        "DBT_DUCKDB_PATH": str(root / "jaffle.duckdb"),
        "DO_NOT_TRACK": "1",
        "DBT_SEND_ANONYMOUS_USAGE_STATS": "false",
    }
    dbt = Path(sys.executable).with_name("dbt")
    done = subprocess.run(
        [str(dbt), "compile", "--profiles-dir", ".", "--target-path", "build_x"],
        cwd=root / "dbt",
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    return {
        "manifest": str(root / "dbt" / "build_x" / "manifest.json"),
        "warehouse": warehouse,
    }


def test_leaky_intermediate_model_makes_its_training_set_leaky(project, capsys):
    argv = ["leakage", "dbt", "--manifest", project["manifest"], "--json"]
    assert exit_code([*argv, "--select", "training_churn_leaky"]) == 1
    (leaky,) = json.loads(capsys.readouterr().out)
    assert leaky["file"] == LEAKY_FILE and leaky["verdict"] == "leaky"
    (finding,) = leaky["findings"]
    assert finding["rule_id"] == "L2"
    assert [m["file"] for m in finding["models"]] == [INT_FILE]


def test_the_clean_training_set_is_clean(project, capsys):
    assert exit_code(["leakage", "dbt", "--manifest", project["manifest"], "--json",
                      "--select", "training_churn_clean"]) == 0  # fmt: skip
    (clean,) = json.loads(capsys.readouterr().out)
    assert clean["verdict"] == "clean" and clean["findings"] == []


def test_checking_everything_reports_both_and_fails(project, capsys):
    assert exit_code(["leakage", "dbt", "--manifest", project["manifest"]]) == 1
    out = capsys.readouterr().out
    assert f"CLEAN         0.00  {CLEAN_FILE}" in out and "LEAKY" in out and INT_FILE in out


def test_replay_of_the_iceberg_sources_agrees_with_the_static_verdicts(
    project, monkeypatch, capsys
):
    warehouse = project["warehouse"]
    monkeypatch.setenv("PYICEBERG_CATALOG__JAFFLE__TYPE", "sql")
    monkeypatch.setenv("PYICEBERG_CATALOG__JAFFLE__URI", f"sqlite:///{warehouse}/catalog.db")
    monkeypatch.setenv("PYICEBERG_CATALOG__JAFFLE__WAREHOUSE", f"file://{warehouse}")
    argv = [
        "leakage", "dbt", "--manifest", project["manifest"], "--json", "--replay",
        "--catalog", "jaffle", "--catalog-namespace", "raw",
    ]  # fmt: skip
    assert exit_code(argv) == 1
    by_file = {e["file"]: e for e in json.loads(capsys.readouterr().out)}
    clean, leaky = by_file[CLEAN_FILE], by_file[LEAKY_FILE]
    assert clean["verdict"] == "clean" and clean["replay"]["status"] == "pass"
    assert clean["replay"]["rows_compared"] >= 20
    assert leaky["verdict"] == "leaky" and leaky["replay"]["status"] == "mismatch"
    assert set(leaky["replay"]["mismatched_columns"]) == {"lifetime_orders", "lifetime_amount"}
