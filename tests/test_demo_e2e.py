"""The pure parts of docker/demo-e2e: command builders, output parsers, the generated data.

The demo itself needs Docker and runs through docker/demo-e2e.sh; nothing here starts a
container, a model or a browser.
"""

from __future__ import annotations

import difflib
import importlib.util
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from sqlguard.mcp_server import LEAKAGE_TOOL, build_config
from sqlguard.safety.tools import STATUS_TOOL, TOOL_NAMES

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "docker" / "demo-e2e"
EXAMPLE = ROOT / "examples" / "dbt-lakehouse"


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


story = load("demo_e2e_story", DEMO / "story.py")
loader = load("demo_e2e_loader", EXAMPLE / "load-lakehouse.py")
lake = load("demo_e2e_lake", DEMO / "lake.py")
record = load("demo_e2e_record", DEMO / "record.py")
agent = load("demo_e2e_agent", DEMO / "agent.py")

# -- the agent's server and the Claude Code mode -------------------------------------------------


def test_the_servers_flags_are_ones_sqlguard_mcp_accepts():
    config = build_config(story.mcp_server_args("http://127.0.0.1:18765"), env={})
    assert config.catalog == "lakehouse" and config.namespace == "prod"
    assert config.scope_namespaces == ("prod",)
    assert config.store is not None and config.audit_log is not None
    assert config.review_url == "http://127.0.0.1:18765"


def test_claude_gets_the_guard_tools_only(tmp_path):
    command = story.claude_command("delete the cancelled orders", tmp_path / "mcp.json")
    flags = {
        flag: command[i + 1]
        for i, flag in enumerate(command)
        if flag.startswith("--") and i + 1 < len(command)
    }
    assert command[:3] == ["claude", "-p", "delete the cancelled orders"]
    assert flags["--tools"] == "" and "--strict-mcp-config" in command  # no built-in tool
    assert flags["--permission-mode"] == "dontAsk"
    allowed = flags["--allowedTools"].split(",")
    denied = flags["--disallowedTools"].split(",")
    assert allowed and all(name.startswith("mcp__lakehouse-guard__") for name in allowed)
    assert not set(allowed) & set(denied)
    for name in ("Bash", "Edit", "Write", "Read", "WebFetch"):
        assert name in denied
    known = {*TOOL_NAMES, STATUS_TOOL, LEAKAGE_TOOL}
    for name in allowed + [d for d in denied if d.startswith("mcp__")]:
        assert name.removeprefix("mcp__lakehouse-guard__") in known


def test_the_mcp_entry_starts_the_server_inside_the_runner():
    args = story.mcp_server_args("http://127.0.0.1:18765")
    entry = story.claude_mcp_config("the-runner", args)["mcpServers"]["lakehouse-guard"]
    assert entry["type"] == "stdio" and entry["command"] == "docker"
    assert entry["args"][:2] == ["exec", "-i"] and "the-runner" in entry["args"]
    assert entry["args"][entry["args"].index("sqlguard-mcp") + 1 :] == args
    assert "env" not in entry  # the runner already holds the catalog settings; none travels here


# -- reading tool output -------------------------------------------------------------------------


def test_replay_summary_says_pass_mismatch_or_nothing():
    ok = {"replay": {"status": "pass", "rows_compared": 127, "points_checked": 4}}
    assert story.replay_summary(ok) == (
        "replay: pass - 127 rows over 4 prediction dates equal what was knowable then"
    )
    bad = {
        "replay": {
            "status": "mismatch",
            "rows_compared": 127,
            "rows_mismatched": 50,
            "points_checked": 4,
            "mismatched_columns": {"orders_7d": 50, "amount_7d": 50},
        }
    }
    assert story.replay_summary(bad) == (
        "replay: MISMATCH - 50 of 127 rows differ at 4 prediction dates (amount_7d, orders_7d)"
    )
    assert story.replay_summary({}) == "replay: not run"


def test_the_table_state_is_read_from_the_catalog_line():
    out = (
        "catalog> prod.orders: 97 rows (27 cancelled); main is on snapshot 2455273240615\n"
        "catalog> branches besides main: none\n"
    )
    assert story.parse_catalog_state(out) == {
        "table": "prod.orders",
        "rows": 97,
        "cancelled": 27,
        "snapshot": "2455273240615",
        "branches": "none",
    }
    left = out.replace("main: none", "main: audit-1f2e")
    assert story.parse_catalog_state(left)["branches"] == "audit-1f2e"
    with pytest.raises(ValueError):
        story.parse_catalog_state("nothing useful")


def test_audit_lines_skip_started_records_and_reviewer_notes():
    records = [
        {"phase": "started", "outcome": None},
        {
            "phase": "completed",
            "outcome": "published",
            "approval": "approved",
            "approver": "demo-reviewer",
            "published": True,
            "action": {"sql": "DELETE FROM prod.orders WHERE id = ?"},
        },
        {"phase": "completed", "outcome": "reviewer_note", "note": "ok"},
    ]
    (line,) = story.audit_lines(records)
    assert line.startswith("published") and "by=demo-reviewer" in line and "DELETE FROM" in line


def test_elapsed_time_reads_in_seconds_then_minutes():
    assert story.elapsed(42.9) == "42s" and story.elapsed(125) == "2m05s"


def test_the_agent_prints_what_a_reader_needs_of_a_held_action():
    payload = {
        "status": "pending_approval",
        "action_id": "ab" * 16,
        "summary": "prod.orders: 0 rows added, 30 deleted, 0 modified (of 100)",
        "reasons": ["r1", "r2", "r3", "r4"],
        "message": "Not applied: it waits. Tell the user its action_id.",
    }
    lines = agent.render_result("run_sql", payload)
    assert lines[0] == "status: pending_approval"
    assert (
        f"action_id: {'ab' * 16}" in lines and "reason: r3" in lines and "reason: r4" not in lines
    )
    assert lines[-1] == "message: Not applied: it waits."
    described = {"table": "prod.orders", "row_count": 100, "current_snapshot_id": 7}
    assert agent.render_result("describe_table", described) == [
        "prod.orders: 100 rows, main is on snapshot 7"
    ]


# -- the data and the project --------------------------------------------------------------------


def test_the_nightly_history_has_late_rows_before_every_prediction_date():
    customers, orders, labels = loader.simulate()
    assert len(customers) == loader.N_CUSTOMERS and labels
    assert all(o["loaded_at"] > o["ordered_at"] for o in orders)
    batches = loader.nightly_batches(orders)
    assert 20 < len(batches) < 40  # a commit per night
    assert [night for night, _ in batches] == sorted(night for night, _ in batches)
    assert all(rows and all(r["loaded_at"] == night for r in rows) for night, rows in batches)
    assert loader.late_rows(orders) > 0
    for offset in loader.CUTOFF_DAYS:
        cutoff = loader.START + timedelta(days=offset)
        late = [o for o in orders if o["ordered_at"] < cutoff <= o["loaded_at"]]
        assert late, f"nothing lands after the prediction date {cutoff:%Y-%m-%d}"
    assert loader.simulate() == (customers, orders, labels)  # fixed seed


def test_the_loader_stamps_each_commit_with_its_night():
    assert loader.LOGICAL_TS == "sqlguard.logical-ts"
    night = datetime(2026, 9, 2, 2)
    assert loader.nightly_batches([{"loaded_at": night, "ordered_at": night}])[0][0] == night


def test_the_seeded_production_table_has_thirty_cancelled_orders():
    rows = lake.order_rows(1, 100)
    assert len(rows) == 100 and sum(r["status"] == "cancelled" for r in rows) == 30
    narrow = [
        r for r in rows if r["status"] == "cancelled" and r["customer_id"] == 1 and r["id"] <= 30
    ]
    assert [r["id"] for r in narrow] == [10, 20, 30]


def test_the_suggested_fix_is_one_added_join_condition():
    before = (EXAMPLE / "models/intermediate/int_customer_orders_7d.sql").read_text().splitlines()
    after = (EXAMPLE / "fix/int_customer_orders_7d.sql").read_text().splitlines()
    changes = [d for d in difflib.ndiff(before, after) if d[0] in "+-"]
    assert changes == ["+     and orders.loaded_at < labels.cutoff_ts"]


def test_the_demo_policy_loads_and_holds_production_writes():
    from sqlguard.safety import Policy

    policy = Policy.load(DEMO / "policy.yaml")
    assert policy.environment_of("prod.orders") == "prod"
    assert policy.require_table_key


# -- the recording --------------------------------------------------------------------------------


def test_the_transcript_drops_colours_and_applies_carriage_returns():
    events = [[0.1, "o", "\x1b[1;36m== 1/11  step\x1b[0m\r\n"], [0.2, "o", "10%\r100%\r\ndone\r\n"]]
    assert record.transcript(events) == "== 1/11  step\n100%\ndone\n"


def test_the_gif_timeline_caps_waits_and_spaces_out_lines():
    events = [[0.0, "o", "a\nb\n"], [30.0, "o", "== 2/11  next\n"], [30.1, "o", "tail"]]
    timeline = record.retime(events, idle_max=0.8, line_delay=0.1)
    stamps = [stamp for stamp, _ in timeline]
    assert [text for _, text in timeline] == ["a\n", "b\n", "== 2/11  next\n", "tail"]
    assert stamps[:2] == [0.0, 0.1]  # one burst arrives line by line
    assert stamps[2] == pytest.approx(0.2 + 0.8)  # a 30 s wait counts for 0.8 s
    assert record.pause_after("== 3/11  headline\n") > record.pause_after("   $ dbt test\n") > 0
    assert record.pause_after("plain output\n") == 0


def test_a_cast_is_written_and_read_back_as_asciicast_v2(tmp_path):
    path = tmp_path / "demo.cast"
    events = [[0.5, "o", "hello\r\n"], [1.25, "o", "\u00e9\r\n"]]
    record.write_cast(path, events, 120, 36, ["docker/demo-e2e.sh"])
    header, back = record.read_cast(path)
    assert header["version"] == 2 and (header["width"], header["height"]) == (120, 36)
    assert back == events
