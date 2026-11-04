"""`LakehouseCatalog.from_config` and the shared catalog flags.

PyIceberg reads its configuration (`~/.pyiceberg.yaml`, `PYICEBERG_CATALOG__*`) when it is
imported, so these tests lean on `from_config` re-reading it at call time: each one points HOME
at a temporary directory and clears the variables that could leak in from the machine.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import pyarrow as pa
import pytest
from pyiceberg.catalog.sql import SqlCatalog

from bench.leakage.world import WorldConfig, build_world
from sqlguard.cli import main
from sqlguard.core.catalog import LakehouseCatalog, add_catalog_args, catalog_from_args, redact

SPEC = str(Path(__file__).resolve().parents[2] / "bench" / "leakage" / "spec.yaml")
NAME = "lab"  # a SQL catalog's tables belong to the catalog name they were created under

CLEAN = """
SELECT l.user_id, l.label_ts, l.churned, SUM(e.amount) AS amt
FROM labels l LEFT JOIN events e ON e.user_id = l.user_id
  AND e.ingested_at < l.label_ts AND e.event_ts >= l.label_ts - INTERVAL 14 DAY
GROUP BY l.user_id, l.label_ts, l.churned
"""
EVENT_TIME_ONLY = CLEAN.replace("e.ingested_at < l.label_ts", "e.event_ts < l.label_ts")


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A HOME with no PyIceberg configuration, and no variables that could add some."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("PYICEBERG_HOME", raising=False)
    monkeypatch.chdir(tmp_path)  # PyIceberg also looks for .pyiceberg.yaml in the cwd
    for name in [n for n in os.environ if n.startswith("PYICEBERG_")]:
        monkeypatch.delenv(name)
    return home


def make_lake(path: Path, namespace: str = "db") -> LakehouseCatalog:
    """A SQL-catalog warehouse as another process would have created it, under catalog `lab`."""
    path.mkdir(parents=True, exist_ok=True)
    catalog = SqlCatalog(NAME, uri=f"sqlite:///{path / 'catalog.db'}", warehouse=f"file://{path}")
    catalog.create_namespace_if_not_exists(namespace)
    return LakehouseCatalog(catalog, namespace)


@pytest.fixture
def warehouse(tmp_path: Path) -> Path:
    """A warehouse holding `db.orders`."""
    path = tmp_path / "warehouse"
    make_lake(path).create_table("orders", pa.table({"id": [1, 2, 3]}))
    return path


def sql_properties(warehouse: Path) -> dict[str, str]:
    return {
        "type": "sql",
        "uri": f"sqlite:///{warehouse / 'catalog.db'}",
        "warehouse": f"file://{warehouse}",
    }


def write_pyiceberg_yaml(directory: Path, warehouse: Path, default: bool = False) -> None:
    props = sql_properties(warehouse)
    (directory / ".pyiceberg.yaml").write_text(
        (f"default-catalog: {NAME}\n" if default else "") + "catalog:\n"
        f"  {NAME}:\n"
        f"    type: {props['type']}\n"
        f"    uri: {props['uri']}\n"
        f"    warehouse: {props['warehouse']}\n",
        encoding="utf-8",
    )


def test_from_config_with_a_properties_dict(home: Path, warehouse: Path) -> None:
    lake = LakehouseCatalog.from_config(NAME, sql_properties(warehouse))
    assert lake.namespace == "db" and lake.table_names() == ["orders"]
    assert lake.read("orders").column("id").to_pylist() == [1, 2, 3]


def test_from_config_with_environment_variables(
    home: Path, warehouse: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key, value in sql_properties(warehouse).items():
        monkeypatch.setenv(f"PYICEBERG_CATALOG__LAB__{key.upper()}", value)
    lake = LakehouseCatalog.from_config(NAME)
    assert lake.table_names() == ["orders"]


def test_from_config_with_the_pyiceberg_yaml_in_home(home: Path, warehouse: Path) -> None:
    write_pyiceberg_yaml(home, warehouse)
    assert LakehouseCatalog.from_config(NAME).table_names() == ["orders"]


def test_the_unnamed_catalog_is_the_configured_default(home: Path, warehouse: Path) -> None:
    write_pyiceberg_yaml(home, warehouse, default=True)
    assert LakehouseCatalog.from_config().table_names() == ["orders"]


def test_properties_override_the_configured_values(home: Path, warehouse: Path) -> None:
    write_pyiceberg_yaml(home, warehouse)
    other = warehouse.parent / "other"
    make_lake(other).create_table("elsewhere", pa.table({"x": [1]}))
    lake = LakehouseCatalog.from_config(NAME, sql_properties(other))
    assert lake.table_names() == ["elsewhere"]


def test_a_namespace_other_than_the_default(home: Path, warehouse: Path) -> None:
    make_lake(warehouse, "sales").create_table("invoices", pa.table({"n": [1]}))
    lake = LakehouseCatalog.from_config(NAME, sql_properties(warehouse), namespace="sales")
    assert lake.table_names() == ["invoices"] and lake.identifier("invoices") == "sales.invoices"


def test_unusable_configuration_is_a_value_error(home: Path, warehouse: Path) -> None:
    with pytest.raises(ValueError, match="namespace 'nope' does not exist"):
        LakehouseCatalog.from_config(NAME, sql_properties(warehouse), namespace="nope")
    with pytest.raises(ValueError, match="nothing"):
        LakehouseCatalog.from_config("nothing")


def test_from_config_does_not_create_anything(home: Path, tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    with pytest.raises(ValueError):
        LakehouseCatalog.from_config(NAME, sql_properties(empty))
    assert not empty.exists()


# -- the shared flags ----------------------------------------------------------------------------


def parse(*argv: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    add_catalog_args(parser)
    return parser.parse_args(argv)


def test_flags_select_a_named_catalog_or_a_local_warehouse(home: Path, warehouse: Path) -> None:
    write_pyiceberg_yaml(home, warehouse)
    local_path = warehouse.parent / "local"
    LakehouseCatalog.local(local_path).create_table("events", pa.table({"x": [1]}))
    named = catalog_from_args(parse("--catalog", NAME))
    local = catalog_from_args(parse("--warehouse", str(local_path)))
    assert named is not None and named.table_names() == ["orders"]
    assert local is not None and local.table_names() == ["events"]
    assert catalog_from_args(parse()) is None


def test_catalog_and_warehouse_exclude_each_other(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        parse("--catalog", "a", "--warehouse", "b")
    assert exit_info.value.code == 2
    assert "not allowed with" in capsys.readouterr().err


# -- end to end: replay on a catalog configured through ~/.pyiceberg.yaml ------------------------


def run(argv: list[str]) -> int:
    with pytest.raises(SystemExit) as exit_info:
        main(argv)
    return exit_info.value.code


@pytest.fixture
def lab(home: Path, tmp_path: Path) -> Path:
    path = tmp_path / "lab-warehouse"
    build_world(make_lake(path), WorldConfig(seed=1, n_users=30, n_days=30))
    write_pyiceberg_yaml(home, path)
    return path


def test_leakage_replay_runs_against_a_catalog_from_pyiceberg_yaml(
    lab: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    clean, leaky = tmp_path / "clean.sql", tmp_path / "leaky.sql"
    clean.write_text(CLEAN, encoding="utf-8")
    leaky.write_text(EVENT_TIME_ONLY, encoding="utf-8")
    base = ["leakage", "check", "--spec", SPEC, "--catalog", NAME, "--replay"]

    assert run([*base, str(clean), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["verdict"] == "clean"
    assert run([*base, str(leaky), "--json"]) == 1
    report = json.loads(capsys.readouterr().out)[0]
    assert report["verdict"] == "leaky" and "REPLAY" in {f["rule_id"] for f in report["findings"]}


def test_a_bad_catalog_name_is_a_usage_error(
    home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sql = tmp_path / "q.sql"
    sql.write_text("SELECT 1", encoding="utf-8")
    argv = ["leakage", "check", str(sql), "--spec", SPEC, "--catalog", "x"]
    assert run(argv) == 2
    assert "cannot open catalog" in capsys.readouterr().err


# -- sqlguard-mcp --------------------------------------------------------------------------------


def test_mcp_config_takes_a_catalog_instead_of_a_warehouse(tmp_path: Path) -> None:
    from sqlguard.mcp_server import build_config

    config = build_config(["--catalog", NAME, "--audit-log", str(tmp_path / "a.jsonl")], {})
    assert config.catalog == NAME and config.warehouse is None

    from_env = build_config([], {"SQLGUARD_CATALOG": NAME})
    assert from_env.catalog == NAME and from_env.warehouse is None
    # a flag for one kind of lakehouse outranks an environment variable for the other
    assert build_config(["--catalog", NAME], {"SQLGUARD_WAREHOUSE": "/w"}).warehouse is None
    assert build_config(["--warehouse", "/w"], {"SQLGUARD_CATALOG": NAME}).catalog is None


@pytest.mark.parametrize("argv", [[], ["--catalog", NAME, "--warehouse", "/w"]])
def test_mcp_config_needs_exactly_one_lakehouse(argv: list[str]) -> None:
    from sqlguard.mcp_server import build_config

    with pytest.raises(SystemExit) as exit_info:
        build_config(argv, {})
    assert exit_info.value.code == 2


def test_mcp_serves_a_catalog_from_pyiceberg_yaml(home: Path, warehouse: Path, tmp_path: Path):
    from sqlguard.mcp_server import ConfigError, ServerConfig, build_tools

    write_pyiceberg_yaml(home, warehouse)
    audit = tmp_path / "audit.jsonl"
    tools = build_tools(ServerConfig(catalog=NAME, audit_log=audit, spec_dir=Path(SPEC).parent))
    assert tools.warehouse is None
    assert tools.backend.catalog.table_names() == ["orders"]
    with pytest.raises(ConfigError, match="--audit-log is required"):
        build_tools(ServerConfig(catalog=NAME))
    with pytest.raises(ConfigError, match="cannot open catalog"):
        build_tools(ServerConfig(catalog="missing", audit_log=audit))
    # without a local warehouse a client cannot point the leakage check at one
    result = tools.call(
        "check_training_sql", {"sql": "SELECT 1", "spec_path": "spec.yaml", "warehouse": "/w"}
    )
    assert result.status == "error" and "server's own warehouse" in json.dumps(result.payload)


# -- credentials never reach the error text ------------------------------------------------------

PASSWORD = "S3cretPw-7731"
# a URI PyIceberg cannot infer a catalog type from; its error message quotes the whole URI
PLANTED_URI = f"postgres://admin:{PASSWORD}@db.internal/iceberg"


@pytest.mark.parametrize(
    "text",
    [
        f"Could not infer the catalog type from the uri: {PLANTED_URI}",
        f"props {{'uri': 'jdbc:mysql://u:{PASSWORD}@h/db', 'x': 1}}",
        f"props {{'credential': '{PASSWORD}', 'token': '{PASSWORD}'}}",
        f"s3.secret-access-key={PASSWORD} password: {PASSWORD} api_key = {PASSWORD}",
    ],
)
def test_redact_removes_credentials(text: str) -> None:
    cleaned = redact(text)
    assert PASSWORD not in cleaned
    assert "***" in cleaned


def test_redact_removes_known_secret_values_wherever_they_appear() -> None:
    assert PASSWORD not in redact(f"odd place: {PASSWORD}!", {PASSWORD})


def test_redact_keeps_the_rest_of_the_message_useful() -> None:
    cleaned = redact(f"cannot reach {PLANTED_URI} (timeout)")
    assert "db.internal/iceberg" in cleaned and "timeout" in cleaned


def test_from_config_errors_and_tracebacks_do_not_carry_the_password(home: Path) -> None:
    with pytest.raises(ValueError) as caught:
        LakehouseCatalog.from_config("probe", {"uri": PLANTED_URI, "credential": PASSWORD})
    assert PASSWORD not in str(caught.value)
    assert caught.value.__cause__ is None and caught.value.__suppress_context__


def test_cli_stderr_does_not_carry_the_password(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("PYICEBERG_CATALOG__PROBE__URI", PLANTED_URI)
    sql = tmp_path / "q.sql"
    sql.write_text("SELECT 1", encoding="utf-8")
    argv = ["leakage", "check", str(sql), "--spec", SPEC, "--catalog", "probe"]
    assert run(argv) == 2
    captured = capsys.readouterr()
    assert "cannot open catalog" in captured.err
    assert PASSWORD not in captured.err + captured.out


def test_mcp_stderr_does_not_carry_the_password(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from sqlguard.mcp_server import main as mcp_main

    monkeypatch.setenv("PYICEBERG_CATALOG__PROBE__URI", PLANTED_URI)
    with pytest.raises(SystemExit) as exit_info:
        mcp_main(["--catalog", "probe", "--audit-log", str(tmp_path / "audit.jsonl")])
    assert exit_info.value.code == 2
    captured = capsys.readouterr()
    assert "cannot open catalog" in captured.err
    assert PASSWORD not in captured.err + captured.out
