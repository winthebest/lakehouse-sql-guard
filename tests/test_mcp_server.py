"""The MCP server, driven through the SDK's in-process client (no subprocess, no network).

The guarded tools are exercised end to end against a throwaway Iceberg warehouse; the one
subprocess test checks that the stdio entry point speaks the protocol.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pyarrow as pa
import pytest
import yaml
from mcp import Client, StdioServerParameters

from sqlguard.core.catalog import LakehouseCatalog
from sqlguard.mcp_server import (
    LEAKAGE_TOOL,
    ConfigError,
    ServerConfig,
    build_config,
    build_server,
    build_tools,
)
from sqlguard.safety.tools import TOOL_NAMES, TOOL_SPECS
from tests.leakage.conftest import SPEC

ROWS = 20
CLEAN = (
    "SELECT l.user_id, l.label_ts, l.churned, s.total_spend FROM labels l "
    "ASOF JOIN user_stats s ON l.user_id = s.user_id AND s.updated_at < l.label_ts"
)
LEAKY = (
    "SELECT l.user_id, l.label_ts, l.churned, s.total_spend FROM labels l "
    "JOIN user_stats s ON s.user_id = l.user_id"
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def warehouse(tmp_path: Path) -> Path:
    path = tmp_path / "warehouse"
    catalog = LakehouseCatalog.local(path)
    catalog.create_table(
        "orders",
        pa.table(
            {
                "id": pa.array(range(1, ROWS + 1), pa.int64()),
                "customer": pa.array([f"c{i % 4}" for i in range(ROWS)]),
                "amount": pa.array([float(i) for i in range(ROWS)]),
            }
        ),
    )
    catalog.create_table(
        "labels",
        pa.table(
            {
                "user_id": pa.array([1], pa.int64()),
                "label_ts": pa.array([datetime(2026, 1, 1)], pa.timestamp("us")),
                "churned": pa.array([0], pa.int64()),
            }
        ),
    )
    catalog.create_table(
        "user_stats",
        pa.table(
            {
                "user_id": pa.array([1], pa.int64()),
                "updated_at": pa.array([datetime(2025, 12, 1)], pa.timestamp("us")),
                "total_spend": pa.array([10.0]),
            }
        ),
    )
    return path


@pytest.fixture
def specs(tmp_path: Path) -> Path:
    directory = tmp_path / "specs"
    directory.mkdir()
    (directory / "churn.yaml").write_text(yaml.safe_dump(SPEC), encoding="utf-8")
    return directory


@pytest.fixture
def config(warehouse: Path, specs: Path, tmp_path: Path) -> ServerConfig:
    return ServerConfig(
        warehouse=warehouse,
        audit_log=tmp_path / "audit.jsonl",
        scope_namespaces=("db",),
        spec_dir=specs,
    )


@pytest.fixture
async def client(config: ServerConfig):
    async with Client(build_server(build_tools(config)), raise_exceptions=True) as connected:
        yield connected


def current_ids(warehouse: Path) -> list[int]:
    return sorted(LakehouseCatalog.local(warehouse).read("orders").column("id").to_pylist())


def audit_records(config: ServerConfig) -> list[dict]:
    assert config.audit_log is not None
    lines = config.audit_log.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]


# -- tool list -----------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_lists_the_guarded_tools_with_the_shared_schemas(client: Client) -> None:
    listed = (await client.list_tools()).tools
    assert [tool.name for tool in listed] == [*TOOL_NAMES, LEAKAGE_TOOL]
    by_name = {tool.name: tool for tool in listed}
    for spec in TOOL_SPECS:
        assert by_name[spec.name].input_schema == spec.parameters
        assert by_name[spec.name].description == spec.description
    assert by_name["list_tables"].annotations.read_only_hint is True
    assert by_name["run_sql"].annotations.read_only_hint is False
    assert by_name["run_sql"].annotations.destructive_hint is True


# -- metadata and reads --------------------------------------------------------------------------


@pytest.mark.anyio
async def test_lists_and_describes_tables(client: Client) -> None:
    tables = await client.call_tool("list_tables", {})
    assert not tables.is_error
    assert tables.structured_content["payload"]["tables"] == [
        "db.labels",
        "db.orders",
        "db.user_stats",
    ]
    described = await client.call_tool("describe_table", {"table": "orders"})
    payload = described.structured_content["payload"]
    assert payload["row_count"] == ROWS
    assert [c["name"] for c in payload["columns"]] == ["id", "customer", "amount"]


@pytest.mark.anyio
async def test_read_returns_rows_as_text_and_structured_content(client: Client) -> None:
    result = await client.call_tool("run_sql", {"sql": "SELECT count(*) AS n FROM orders"})
    assert not result.is_error
    assert json.loads(result.content[0].text)["rows"] == [{"n": ROWS}]
    assert result.structured_content["status"] == "ok"
    assert result.structured_content["guard"]["outcome"] == "read"


# -- writes go through the guard -----------------------------------------------------------------


@pytest.mark.anyio
async def test_small_write_is_rehearsed_and_published(
    client: Client, warehouse: Path, config: ServerConfig
) -> None:
    result = await client.call_tool("run_sql", {"sql": "DELETE FROM orders WHERE id = 20"})
    assert result.structured_content["status"] == "ok"
    assert result.structured_content["guard"]["published"] is True
    assert current_ids(warehouse) == list(range(1, ROWS))
    assert audit_records(config)  # the guard wrote its trail


@pytest.mark.anyio
async def test_wrong_delete_is_held_with_its_impact_and_nothing_runs(
    client: Client, warehouse: Path, config: ServerConfig
) -> None:
    result = await client.call_tool("run_sql", {"sql": "DELETE FROM orders"})
    assert not result.is_error  # information for the agent, not a failure
    structured = result.structured_content
    assert structured["status"] == "needs_approval"
    assert structured["approvals"][0]["approved"] is False
    payload = structured["payload"]
    assert payload["outcome"] == "awaiting_approval"
    assert payload["impact"][0]["rows_deleted"] == ROWS
    assert "no human reviewer" in " ".join(payload["reasons"])
    assert structured["guard"]["outcome"] == "awaiting_approval"
    assert structured["guard"]["published"] is False
    assert current_ids(warehouse) == list(range(1, ROWS + 1))


@pytest.mark.anyio
async def test_a_held_action_is_never_approved_on_retry(client: Client, warehouse: Path) -> None:
    for _ in range(2):
        result = await client.call_tool("run_sql", {"sql": "DELETE FROM orders"})
        assert result.structured_content["status"] == "needs_approval"
    assert current_ids(warehouse) == list(range(1, ROWS + 1))


@pytest.mark.anyio
async def test_out_of_scope_write_is_blocked(warehouse: Path, specs: Path, tmp_path: Path) -> None:
    config = ServerConfig(
        warehouse=warehouse,
        audit_log=tmp_path / "audit.jsonl",
        scope_tables=("db.labels",),
        spec_dir=specs,
    )
    async with Client(build_server(build_tools(config)), raise_exceptions=True) as scoped:
        result = await scoped.call_tool("run_sql", {"sql": "DELETE FROM orders WHERE id = 1"})
    assert result.structured_content["status"] == "blocked"
    assert current_ids(warehouse) == list(range(1, ROWS + 1))


@pytest.mark.anyio
async def test_expire_snapshots_is_planned_and_needs_approval(
    client: Client, warehouse: Path
) -> None:
    catalog = LakehouseCatalog.local(warehouse)
    before = [s.snapshot_id for s in catalog.snapshots("orders")]
    cutoff = (datetime.now(UTC) - timedelta(days=60)).isoformat()
    result = await client.call_tool(
        "expire_snapshots", {"table": "orders", "older_than": cutoff, "retain_last": 5}
    )
    assert result.structured_content["status"] == "needs_approval"
    assert result.structured_content["payload"]["plan"]["kind"] == "expire_snapshots"
    assert [s.snapshot_id for s in catalog.snapshots("orders")] == before


@pytest.mark.anyio
async def test_expire_inside_the_retention_window_is_blocked(client: Client) -> None:
    cutoff = datetime.now(UTC).isoformat()
    result = await client.call_tool(
        "expire_snapshots", {"table": "orders", "older_than": cutoff, "retain_last": 1}
    )
    assert result.structured_content["status"] == "blocked"
    assert any("retention" in r for r in result.structured_content["payload"]["reasons"])


# -- malformed calls -----------------------------------------------------------------------------


@pytest.mark.anyio
async def test_bad_arguments_are_a_tool_error(client: Client) -> None:
    result = await client.call_tool("run_sql", {"query": "SELECT 1"})
    assert result.is_error
    assert result.structured_content["status"] == "invalid_arguments"


@pytest.mark.anyio
async def test_unknown_tool_is_a_tool_error(client: Client) -> None:
    result = await client.call_tool("drop_everything", {})
    assert result.is_error
    assert "unknown tool" in result.content[0].text


# -- leakage tool --------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_leakage_check_flags_a_leaky_join_and_passes_an_asof_join(client: Client) -> None:
    leaky = await client.call_tool(LEAKAGE_TOOL, {"sql": LEAKY, "spec_path": "churn.yaml"})
    assert not leaky.is_error
    assert leaky.structured_content["payload"]["verdict"] == "leaky"
    clean = await client.call_tool(LEAKAGE_TOOL, {"sql": CLEAN, "spec_path": "churn.yaml"})
    assert clean.structured_content["payload"]["verdict"] == "clean"


@pytest.mark.anyio
async def test_leakage_check_can_replay_against_the_warehouse(
    client: Client, warehouse: Path
) -> None:
    result = await client.call_tool(
        LEAKAGE_TOOL,
        {"sql": CLEAN, "spec_path": "churn.yaml", "warehouse": str(warehouse), "replay": True},
    )
    assert not result.is_error
    assert result.structured_content["payload"]["replay"] is not None


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("arguments", "needle"),
    [
        ({"spec_path": "../outside.yaml"}, "inside the server's spec directory"),
        ({"spec_path": "/etc/passwd"}, "inside the server's spec directory"),
        ({"spec_path": "churn.txt"}, ".yaml or .yml"),
        ({"spec_path": "missing.yaml"}, "no spec file"),
        ({"spec_path": "churn.yaml", "warehouse": "/tmp/elsewhere"}, "server's own warehouse"),
        ({"spec_path": "churn.yaml", "replay": "yes"}, "replay must be a boolean"),
        ({"spec_path": "churn.yaml", "extra": 1}, "unexpected argument"),
        ({"spec_path": ""}, "spec_path must be a non-empty string"),
    ],
)
async def test_leakage_check_rejects_paths_outside_its_sandbox(
    client: Client, arguments: dict, needle: str
) -> None:
    result = await client.call_tool(LEAKAGE_TOOL, {"sql": CLEAN, **arguments})
    assert result.is_error
    assert needle in result.content[0].text


@pytest.mark.anyio
async def test_leakage_check_does_not_follow_a_symlink_out_of_the_spec_dir(
    client: Client, specs: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside.yaml"
    outside.write_text(yaml.safe_dump(SPEC), encoding="utf-8")
    (specs / "link.yaml").symlink_to(outside)
    result = await client.call_tool(LEAKAGE_TOOL, {"sql": CLEAN, "spec_path": "link.yaml"})
    assert result.is_error


@pytest.mark.anyio
async def test_leakage_check_reports_an_invalid_spec(client: Client, specs: Path) -> None:
    (specs / "bad.yaml").write_text("- not a mapping\n", encoding="utf-8")
    result = await client.call_tool(LEAKAGE_TOOL, {"sql": CLEAN, "spec_path": "bad.yaml"})
    assert result.is_error
    assert "mapping" in result.content[0].text


# -- configuration -------------------------------------------------------------------------------


def test_config_comes_from_flags_then_environment(tmp_path: Path) -> None:
    env = {
        "SQLGUARD_WAREHOUSE": "/env/warehouse",
        "SQLGUARD_SCOPE_TABLES": "orders, db.labels",
        "SQLGUARD_SCOPE_NAMESPACES": "db",
        "SQLGUARD_MAX_ROWS": "7",
    }
    config = build_config(["--audit-log", str(tmp_path / "a.jsonl")], env)
    assert config.warehouse == Path("/env/warehouse")
    assert config.scope_tables == ("orders", "db.labels")
    assert config.scope_namespaces == ("db",)
    assert config.max_rows == 7
    assert config.audit_log == tmp_path / "a.jsonl"

    flagged = build_config(["--warehouse", "/flag", "--scope-table", "x"], env)
    assert flagged.warehouse == Path("/flag")
    assert flagged.scope_tables == ("x",)


def test_config_requires_a_warehouse() -> None:
    with pytest.raises(SystemExit) as exit_info:
        build_config([], {})
    assert exit_info.value.code == 2


def test_missing_warehouse_or_bad_policy_stops_startup(warehouse: Path, tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="no Iceberg warehouse"):
        build_tools(ServerConfig(warehouse=tmp_path / "nowhere"))
    bad_policy = tmp_path / "policy.yaml"
    bad_policy.write_text("class_decisions: {read: deny, typo: allow}\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="cannot load policy"):
        build_tools(ServerConfig(warehouse=warehouse, policy=bad_policy))
    with pytest.raises(ConfigError, match="cannot load policy"):
        build_tools(ServerConfig(warehouse=warehouse, policy=tmp_path / "absent.yaml"))


# -- stdio ---------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_stdio_entry_point_serves_the_tools(
    warehouse: Path, specs: Path, tmp_path: Path
) -> None:
    params = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "sqlguard.mcp_server",
            "--warehouse",
            str(warehouse),
            "--audit-log",
            str(tmp_path / "audit.jsonl"),
            "--scope-namespace",
            "db",
            "--spec-dir",
            str(specs),
        ],
    )
    async with Client(params) as stdio_client:
        listed = await stdio_client.list_tools()
        assert LEAKAGE_TOOL in [tool.name for tool in listed.tools]
        held = await stdio_client.call_tool("run_sql", {"sql": "DELETE FROM orders"})
        assert held.structured_content["status"] == "needs_approval"
    assert current_ids(warehouse) == list(range(1, ROWS + 1))
