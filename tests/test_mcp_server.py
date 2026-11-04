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
    DEFAULT_REQUESTER,
    INSTRUCTIONS,
    LEAKAGE_TOOL,
    ConfigError,
    ServerConfig,
    build_config,
    build_server,
    build_tools,
)
from sqlguard.safety import ActionState, ActionStore, Guard, Outcome, Policy, SqlAction, TaskScope
from sqlguard.safety import tools as tools_module
from sqlguard.safety.tools import STATUS_TOOL, TOOL_NAMES, TOOL_SPECS
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
        store=tmp_path / "actions.db",
        review_url="http://127.0.0.1:8765",
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
    assert [tool.name for tool in listed] == [*TOOL_NAMES, STATUS_TOOL, LEAKAGE_TOOL]
    by_name = {tool.name: tool for tool in listed}
    for spec in TOOL_SPECS:
        assert by_name[spec.name].input_schema == spec.parameters
        assert by_name[spec.name].description == spec.description
    assert by_name["list_tables"].annotations.read_only_hint is True
    assert by_name["run_sql"].annotations.read_only_hint is False
    assert by_name["run_sql"].annotations.destructive_hint is True
    assert by_name[STATUS_TOOL].annotations.read_only_hint is True


@pytest.mark.anyio
async def test_no_tool_can_approve_and_the_server_holds_nothing_to_approve_with(
    client: Client, config: ServerConfig
) -> None:
    names = [tool.name for tool in (await client.list_tools()).tools]
    assert not [n for n in names if any(w in n for w in ("approve", "resolve", "decide", "reject"))]
    guard = build_tools(config).backend.guard
    assert guard.proof_verifier is None
    assert guard.requester == DEFAULT_REQUESTER
    with pytest.raises(Exception, match="no proof verifier"):
        guard.resolve("0" * 32, "approve", "agent", "proof")


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
    assert structured["status"] == "pending_approval"
    assert structured["approvals"] == []
    payload = structured["payload"]
    assert payload["status"] == "pending_approval" and payload["outcome"] == "awaiting_approval"
    assert payload["impact"][0]["rows_deleted"] == ROWS
    assert "waiting for a reviewer" in payload["message"]
    assert structured["guard"]["outcome"] == "awaiting_approval"
    assert structured["guard"]["published"] is False
    assert current_ids(warehouse) == list(range(1, ROWS + 1))

    # the short form the agent relays to the user
    record = ActionStore(config.store).get(payload["action_id"])
    assert record is not None and record.state is ActionState.PENDING
    assert record.requester == DEFAULT_REQUESTER
    assert payload["action_hash_prefix"] == record.action_hash[:6]
    assert payload["expires_at"] == record.expires_at.isoformat()
    assert payload["review_url"] == "http://127.0.0.1:8765"
    assert payload["summary"] == f"db.orders: 0 rows added, {ROWS} deleted (of {ROWS})"
    assert "action_hash" not in payload and record.action_hash not in json.dumps(structured)
    assert len(result.content[0].text) < 4000


@pytest.mark.anyio
async def test_without_a_review_url_the_field_is_left_out(
    warehouse: Path, specs: Path, tmp_path: Path
) -> None:
    config = ServerConfig(
        warehouse=warehouse,
        audit_log=tmp_path / "audit.jsonl",
        store=tmp_path / "actions.db",
        scope_namespaces=("db",),
        spec_dir=specs,
    )
    async with Client(build_server(build_tools(config)), raise_exceptions=True) as bare:
        held = await bare.call_tool("run_sql", {"sql": "DELETE FROM orders"})
    assert "review_url" not in held.structured_content["payload"]


@pytest.mark.anyio
async def test_a_held_action_is_never_approved_on_retry(client: Client, warehouse: Path) -> None:
    for _ in range(2):
        result = await client.call_tool("run_sql", {"sql": "DELETE FROM orders"})
        assert result.structured_content["status"] == "pending_approval"
    assert current_ids(warehouse) == list(range(1, ROWS + 1))


@pytest.mark.anyio
async def test_the_requester_label_is_fixed_by_the_server_and_bounds_the_queue(
    warehouse: Path, specs: Path, tmp_path: Path
) -> None:
    config = ServerConfig(
        warehouse=warehouse,
        audit_log=tmp_path / "audit.jsonl",
        store=tmp_path / "actions.db",
        requester="ide-agent",
        scope_namespaces=("db",),
        spec_dir=specs,
    )
    async with Client(build_server(build_tools(config)), raise_exceptions=True) as capped:
        statuses = []
        for _ in range(11):
            result = await capped.call_tool("run_sql", {"sql": "DELETE FROM orders"})
            statuses.append(result.structured_content["status"])
    assert statuses == ["pending_approval"] * 10 + ["blocked"]
    assert {r.requester for r in ActionStore(config.store).list_actions()} == {"ide-agent"}
    assert any("ide-agent" in r for r in result.structured_content["payload"]["reasons"])
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
async def test_expire_snapshots_is_planned_and_pending_approval(
    client: Client, warehouse: Path
) -> None:
    catalog = LakehouseCatalog.local(warehouse)
    before = [s.snapshot_id for s in catalog.snapshots("orders")]
    cutoff = (datetime.now(UTC) - timedelta(days=60)).isoformat()
    result = await client.call_tool(
        "expire_snapshots", {"table": "orders", "older_than": cutoff, "retain_last": 5}
    )
    assert result.structured_content["status"] == "pending_approval"
    assert result.structured_content["payload"]["plan"]["kind"] == "expire_snapshots"
    assert result.structured_content["payload"]["action_id"]
    assert [s.snapshot_id for s in catalog.snapshots("orders")] == before


@pytest.mark.anyio
async def test_expire_inside_the_retention_window_is_blocked(client: Client) -> None:
    cutoff = datetime.now(UTC).isoformat()
    result = await client.call_tool(
        "expire_snapshots", {"table": "orders", "older_than": cutoff, "retain_last": 1}
    )
    assert result.structured_content["status"] == "blocked"
    assert any("retention" in r for r in result.structured_content["payload"]["reasons"])


# -- get_action_status --------------------------------------------------------------------------


class AcceptAnything:
    def verify(self, request, proof) -> bool:
        return True


def reviewer(warehouse: Path, config: ServerConfig, requester: str = "person") -> Guard:
    """The person's side: the same store and catalog, plus the means to decide."""
    return Guard(
        LakehouseCatalog.local(warehouse),
        Policy(),
        audit_path=config.audit_log,
        store=config.store,
        proof_verifier=AcceptAnything(),
        requester=requester,
    )


async def hold(client: Client, sql: str = "DELETE FROM orders") -> str:
    held = await client.call_tool("run_sql", {"sql": sql})
    assert held.structured_content["status"] == "pending_approval"
    return held.structured_content["payload"]["action_id"]


async def state_of(client: Client, action_id: str) -> dict:
    result = await client.call_tool(STATUS_TOOL, {"action_id": action_id})
    assert not result.is_error
    return result.structured_content["payload"]


@pytest.mark.anyio
async def test_status_follows_an_action_from_pending_to_published(
    client: Client, warehouse: Path, config: ServerConfig
) -> None:
    action_id = await hold(client, "DELETE FROM orders WHERE id <= 15")
    waiting = await state_of(client, action_id)
    assert waiting["state"] == "pending" and waiting["table"] == "db.orders"
    assert waiting["summary"].startswith("db.orders: 0 rows added, 15 deleted")
    assert "Nothing has been applied" in waiting["message"]
    assert "DELETE" not in json.dumps(waiting)  # the statement text stays with the reviewer

    done = reviewer(warehouse, config).resolve(action_id, "approve", "alice", "proof")
    assert done.outcome is Outcome.PUBLISHED
    after = await state_of(client, action_id)
    assert after["state"] == "published" and "applied" in after["message"]
    assert current_ids(warehouse) == list(range(16, ROWS + 1))


@pytest.mark.anyio
async def test_status_reports_a_rejected_action(
    client: Client, warehouse: Path, config: ServerConfig
) -> None:
    action_id = await hold(client)
    reviewer(warehouse, config).resolve(action_id, "reject", "alice", "proof")
    rejected = await state_of(client, action_id)
    assert rejected["state"] == "rejected" and "Nothing was applied" in rejected["message"]
    assert current_ids(warehouse) == list(range(1, ROWS + 1))


@pytest.mark.anyio
async def test_status_reports_an_action_that_went_stale(
    client: Client, warehouse: Path, config: ServerConfig
) -> None:
    action_id = await hold(client)
    LakehouseCatalog.local(warehouse).table("orders").delete("id = 1")  # main moves
    stale = reviewer(warehouse, config).resolve(action_id, "approve", "alice", "proof")
    assert stale.outcome is Outcome.STALE
    assert (await state_of(client, action_id))["state"] == "stale"


@pytest.mark.anyio
async def test_status_only_shows_actions_this_server_queued(
    client: Client, warehouse: Path, config: ServerConfig
) -> None:
    other = reviewer(warehouse, config, requester="someone-else").submit(
        SqlAction("DELETE FROM orders"), TaskScope.of(namespaces=["db"])
    )
    assert other.action_id
    unknown = await client.call_tool(STATUS_TOOL, {"action_id": other.action_id})
    assert unknown.is_error and "no action with this id" in unknown.content[0].text
    nothing = await client.call_tool(STATUS_TOOL, {"action_id": "0" * 32})
    assert nothing.is_error and "no action with this id" in nothing.content[0].text


@pytest.mark.anyio
async def test_status_rejects_malformed_calls(client: Client) -> None:
    for arguments in ({}, {"action_id": ""}, {"action_id": "x", "extra": 1}):
        result = await client.call_tool(STATUS_TOOL, arguments)
        assert result.is_error and result.structured_content["status"] == "invalid_arguments"


@pytest.mark.anyio
async def test_the_instructions_name_the_new_status_and_the_status_tool() -> None:
    assert "pending_approval" in INSTRUCTIONS and "needs_approval" not in INSTRUCTIONS
    assert STATUS_TOOL in INSTRUCTIONS and "cannot approve" in INSTRUCTIONS


# -- bounded output -----------------------------------------------------------------------------


def many_statements(count: int) -> str:
    return "; ".join(f"DELETE FROM orders WHERE id = {i % ROWS + 1}" for i in range(count))


@pytest.mark.anyio
async def test_a_held_action_with_1500_statements_still_returns_a_short_result(
    client: Client, warehouse: Path
) -> None:
    result = await client.call_tool("run_sql", {"sql": many_statements(1500)})
    structured = result.structured_content
    assert structured["status"] == "pending_approval"
    text = result.content[0].text
    assert len(text) < 6000 and len(json.dumps(structured)) < 12000
    payload = structured["payload"]
    assert list(payload)[:3] == ["status", "action_id", "summary"]  # what the agent needs, first
    reasons = payload["reasons"]
    assert len(reasons) == tools_module.MAX_REASONS + 1
    assert reasons[-1].startswith("... and ") and reasons[-1].endswith(" more reasons")
    assert len(set(reasons)) == len(reasons)
    assert structured["guard"]["reasons"] == reasons
    assert current_ids(warehouse) == list(range(1, ROWS + 1))


@pytest.mark.anyio
async def test_any_result_over_the_cap_is_cut_to_its_essentials(
    client: Client, warehouse: Path
) -> None:
    wide = LakehouseCatalog.local(warehouse)
    wide.create_table(
        "notes", pa.table({"id": pa.array([1, 2], pa.int64()), "text": ["x" * 30000] * 2})
    )
    result = await client.call_tool("run_sql", {"sql": "SELECT * FROM notes"})
    assert not result.is_error
    assert len(json.dumps(result.structured_content)) < tools_module.MAX_RESULT_CHARS
    payload = result.structured_content["payload"]
    assert payload["status"] == "ok" and "too large to return" in payload["truncated"]
    assert "rows" not in payload


def test_reasons_are_deduplicated_and_counted() -> None:
    assert tools_module.bounded_reasons(["a", "a", "b"]) == ["a", "b"]
    many = [f"r{i}" for i in range(25)]
    shown = tools_module.bounded_reasons(many)
    assert shown[:10] == many[:10] and shown[10] == "... and 15 more reasons"


# -- the status tool's errors --------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_store_failure_is_generic_to_the_agent_and_detailed_in_the_log(
    client: Client,
    config: ServerConfig,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from sqlguard.safety.store import StoreError

    def broken(self, action_id):
        raise StoreError(f"cannot read action {action_id} in {config.store}")

    monkeypatch.setattr(ActionStore, "get", broken)
    with caplog.at_level("WARNING", logger="sqlguard.tools"):
        result = await client.call_tool(STATUS_TOOL, {"action_id": "a" * 32})
    assert result.is_error
    assert str(config.store) not in json.dumps(result.structured_content)
    assert "could not be read" in result.content[0].text
    assert str(config.store) in caplog.text  # the detail stays in the server's log


# -- startup housekeeping ------------------------------------------------------------------------


def test_startup_expires_actions_nobody_decided_and_drops_their_branches(
    warehouse: Path, config: ServerConfig
) -> None:
    late = Guard(
        LakehouseCatalog.local(warehouse),
        Policy(),
        audit_path=config.audit_log,
        store=config.store,
        pending_ttl=timedelta(seconds=-5),
    )
    held = late.submit(SqlAction("DELETE FROM orders"), TaskScope.of(namespaces=["db"]))
    assert held.action_id and ActionStore(config.store).get(held.action_id).state.value == "pending"
    assert len(LakehouseCatalog.local(warehouse).table("orders").refs()) == 2  # main + audit

    build_tools(config)

    assert ActionStore(config.store).get(held.action_id).state is ActionState.EXPIRED
    assert set(LakehouseCatalog.local(warehouse).table("orders").refs()) == {"main"}


def test_startup_reconciles_then_expires_and_serves_even_if_that_fails(
    monkeypatch: pytest.MonkeyPatch, config: ServerConfig, caplog: pytest.LogCaptureFixture
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(Guard, "reconcile", lambda self, *a, **k: calls.append("reconcile") or [])
    monkeypatch.setattr(Guard, "expire_pending", lambda self, *a, **k: calls.append("expire") or [])
    build_tools(config)
    assert calls == ["reconcile", "expire"]

    def broken(self, *args, **kwargs):
        raise RuntimeError("catalog unreachable")

    monkeypatch.setattr(Guard, "reconcile", broken)
    with caplog.at_level("WARNING", logger="sqlguard.mcp"):
        assert build_tools(config).backend.supports_status
    assert "could not settle earlier actions" in caplog.text


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


def test_store_review_url_and_requester_come_from_flags_then_environment(tmp_path: Path) -> None:
    env = {
        "SQLGUARD_WAREHOUSE": "/w",
        "SQLGUARD_STORE": "/env/actions.db",
        "SQLGUARD_REVIEW_URL": "http://127.0.0.1:9000/",
        "SQLGUARD_REQUESTER": "from-env",
    }
    config = build_config([], env)
    assert config.store == Path("/env/actions.db")
    assert config.review_url == "http://127.0.0.1:9000"
    assert config.requester == "from-env"
    flagged = build_config(
        [
            "--store",
            str(tmp_path / "s.db"),
            "--review-url",
            "https://localhost:9443",
            "--requester",
            "x",
        ],
        env,
    )
    assert (flagged.store, flagged.review_url, flagged.requester) == (
        tmp_path / "s.db",
        "https://localhost:9443",
        "x",
    )
    assert build_config([], {"SQLGUARD_WAREHOUSE": "/w"}).requester == DEFAULT_REQUESTER


@pytest.mark.parametrize(
    "flag",
    [
        ["--review-url", "ftp://127.0.0.1/"],
        ["--review-url", "http://user:secret@127.0.0.1/"],
        ["--review-url", "http://127.0.0.1/?token=1"],
        ["--review-url", "http://127.0.0.1/#frag"],
        ["--review-url", "http://127.0.0.1:99999"],
        ["--review-url", "http://review.example.com:8765"],
        ["--review-url", "not a url"],
        ["--requester", "   "],
        ["--requester", "x" * 65],
    ],
)
def test_a_bad_review_url_or_requester_stops_startup(flag: list[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        build_config(["--warehouse", "/w", *flag], {})
    assert exit_info.value.code == 2


SECRET_PREFIX = "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789-_AbCd"


@pytest.mark.parametrize(
    "url",
    [
        f"http://127.0.0.1:8765/s/{SECRET_PREFIX}",
        f"http://127.0.0.1:8765/s/{SECRET_PREFIX}/",
        f"http://127.0.0.1:8765/s/{SECRET_PREFIX}/login?token=abc",
        "http://127.0.0.1:8765/anything",
        f"http://localhost:8765/s/{SECRET_PREFIX}/",
    ],
)
def test_a_review_url_with_a_path_is_refused_and_the_secret_is_not_echoed(
    url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        build_config(["--warehouse", "/w", "--review-url", url], {})
    assert exit_info.value.code == 2
    err = capsys.readouterr().err
    assert "server address only" in err and "never paste" in err
    assert SECRET_PREFIX not in err  # the refusal does not repeat what it refuses


@pytest.mark.parametrize(
    ("given", "kept"),
    [
        ("http://127.0.0.1:8765", "http://127.0.0.1:8765"),
        ("http://127.0.0.1:8765/", "http://127.0.0.1:8765"),
        ("HTTPS://LocalHost:9443", "https://localhost:9443"),
        ("http://[::1]:8765/", "http://[::1]:8765"),
        ("http://localhost", "http://localhost"),
    ],
)
def test_a_review_url_is_kept_as_scheme_host_and_port(given: str, kept: str) -> None:
    assert build_config(["--warehouse", "/w", "--review-url", given], {}).review_url == kept


def test_the_store_defaults_to_the_warehouse_and_is_private(warehouse: Path) -> None:
    tools = build_tools(ServerConfig(warehouse=warehouse))
    store = tools.backend.guard.store
    assert store is not None and store.path == warehouse.resolve() / "sqlguard-actions.db"
    assert (store.path.stat().st_mode & 0o777) == 0o600


def test_an_unusable_store_stops_startup(warehouse: Path, tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    with pytest.raises(ConfigError, match="cannot open action store"):
        build_tools(ServerConfig(warehouse=warehouse, store=blocker / "actions.db"))


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
        assert held.structured_content["status"] == "pending_approval"
    assert current_ids(warehouse) == list(range(1, ROWS + 1))
