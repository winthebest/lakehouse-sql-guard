"""The IDE files and the install commands in docs/mcp.md match what the package really ships.

Nothing is installed or run: the commands are checked against `pyproject.toml` (package name,
extra, entry point) and parsed with the server's own argument parser.
"""

from __future__ import annotations

import json
import re
import shlex
import tomllib
from pathlib import Path

import pytest

from sqlguard.mcp_server import LEAKAGE_TOOL, build_config
from sqlguard.safety.tools import STATUS_TOOL, TOOL_NAMES

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples" / "mcp"
DOC = ROOT / "docs" / "mcp.md"
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
SERVER = "lakehouse-guard"
TAG = re.compile(r"@v\d+\.\d+\.\d+$")
ENTRY_POINT = "sqlguard-mcp"
EXPANSIONS = {
    "${HOME}": "/home/me",
    "${userHome}": "/home/me",
    "${CLAUDE_PROJECT_DIR:-.}": "/work/project",
    "${workspaceFolder}": "/work/project",
}
FILES = {
    "claude-code.mcp.json": "mcpServers",
    "cursor.mcp.json": "mcpServers",
    "claude_desktop_config.json": "mcpServers",
}


def expand(text: str) -> str:
    for placeholder, value in EXPANSIONS.items():
        text = text.replace(placeholder, value)
    return text


def check_from_requirement(requirement: str) -> None:
    """`lakehouse-sql-guard[mcp] @ git+https://...@vX.Y.Z` names this package and a real extra."""
    match = re.fullmatch(r"([\w-]+)\[([\w,-]+)\] @ (git\+https://\S+)", requirement)
    assert match, requirement
    name, extras, url = match.groups()
    assert name == PYPROJECT["project"]["name"]
    assert set(extras.split(",")) <= set(PYPROJECT["project"]["optional-dependencies"])
    assert "mcp" in extras.split(",")
    assert TAG.search(url), "pin the install to a release tag"


def check_server_arguments(args: list[str]) -> None:
    """`--from REQ sqlguard-mcp FLAGS...`: a real entry point followed by flags the server takes."""
    assert args[0] == "--from"
    check_from_requirement(args[1])
    assert args[2] == ENTRY_POINT
    assert ENTRY_POINT in PYPROJECT["project"]["scripts"]
    config = build_config([expand(a) for a in args[3:]], {})
    assert config.store is not None and config.audit_log is not None
    assert config.review_url and config.requester != "mcp"
    assert config.policy is not None


@pytest.mark.parametrize("name", FILES)
def test_each_ide_file_installs_the_real_entry_point_with_flags_the_server_takes(name: str) -> None:
    data = json.loads((EXAMPLES / name).read_text(encoding="utf-8"))
    assert list(data[FILES[name]]) == [SERVER]
    entry = data[FILES[name]][SERVER]
    assert entry["command"] == "uvx"
    check_server_arguments(entry["args"])


@pytest.mark.parametrize("name", ["claude-code.mcp.json", "cursor.mcp.json"])
def test_catalog_settings_go_through_env_and_no_file_holds_a_secret(name: str) -> None:
    text = (EXAMPLES / name).read_text(encoding="utf-8")
    env = json.loads(text)["mcpServers"][SERVER]["env"]
    assert all(key.startswith("PYICEBERG_CATALOG__") for key in env)
    assert all(re.fullmatch(r"\$\{(env:)?[A-Z_]+\}", value) for value in env.values())


def test_the_project_files_keep_the_store_and_policy_out_of_the_project() -> None:
    for name in ("claude-code.mcp.json", "cursor.mcp.json"):
        args = json.loads((EXAMPLES / name).read_text(encoding="utf-8"))["mcpServers"][SERVER][
            "args"
        ]
        config = build_config([expand(a) for a in args[3:]], {})
        for path in (config.store, config.audit_log, config.policy):
            assert path is not None and str(path).startswith("/home/me/")


# -- Claude Code permissions ---------------------------------------------------------------------


@pytest.fixture(scope="module")
def permissions() -> dict[str, list[str]]:
    return json.loads((EXAMPLES / "claude-code.settings.json").read_text(encoding="utf-8"))[
        "permissions"
    ]


def test_the_deny_list_has_the_rules_that_stop_self_approval(permissions) -> None:
    deny = set(permissions["deny"])
    assert {
        "Bash(sqlguard approvals approve:*)",
        "Bash(sqlguard serve:*)",
        "Bash(sqlite3:*)",
        "Read(~/.pyiceberg.yaml)",
    } <= deny
    # the store and the credential file named in the project file are the ones denied
    args = json.loads((EXAMPLES / "claude-code.mcp.json").read_text(encoding="utf-8"))[
        "mcpServers"
    ][SERVER]["args"]
    config = build_config([expand(a) for a in args[3:]], {})
    for path in (config.store, config.audit_log):
        assert path is not None
        assert f"Read(~/{path.parent.relative_to('/home/me')}/**)" in deny
        assert f"Edit(~/{path.parent.relative_to('/home/me')}/**)" in deny
    assert f"Edit(~/{config.policy.parent.relative_to('/home/me')}/**)" in deny


def test_only_read_only_tools_are_allowed_without_asking(permissions) -> None:
    server_tools = {*TOOL_NAMES, STATUS_TOOL, LEAKAGE_TOOL}
    allowed = {rule.removeprefix(f"mcp__{SERVER}__") for rule in permissions["allow"]}
    assert allowed <= server_tools
    assert allowed == {"list_tables", "describe_table", STATUS_TOOL, LEAKAGE_TOOL}
    assert not any(token in tool for tool in allowed for token in ("approve", "resolve"))


# -- the commands in docs/mcp.md -----------------------------------------------------------------


def shell_commands() -> list[list[str]]:
    """Every command in a ```bash block of docs/mcp.md, continuation lines joined."""
    commands = []
    for block in re.findall(r"```bash\n(.*?)```", DOC.read_text(encoding="utf-8"), re.DOTALL):
        commands += [shlex.split(line) for line in block.replace("\\\n", " ").splitlines() if line]
    return commands


def test_the_documented_install_command_uses_the_real_entry_point_and_extra() -> None:
    installs = [c for c in shell_commands() if c[:3] == ["claude", "mcp", "add"]]
    assert len(installs) == 1
    command = installs[0]
    assert command[3:8] == ["--transport", "stdio", "--scope", "project", SERVER]
    assert command[8:10] == ["--", "uvx"]
    args = command[10:]
    env = {"HOME": "/home/me"}
    flags = [a.replace("$HOME", env["HOME"]) for a in args]
    check_server_arguments(flags)


def test_the_documented_approval_commands_take_the_flags_the_cli_has() -> None:
    from sqlguard.cli import approvals as cli_approvals  # noqa: F401  (the commands exist)

    serve = next(c for c in shell_commands() if c[:2] == ["sqlguard", "serve"])
    listing = next(c for c in shell_commands() if c[:3] == ["sqlguard", "approvals", "list"])
    assert {"--store", "--policy", "--audit-log", "--catalog-namespace"} <= set(serve)
    assert "--store" in listing


def test_the_documented_flags_are_all_in_the_server_options_table() -> None:
    text = DOC.read_text(encoding="utf-8")
    table = text.split("## Server options", 1)[1].split("\n## ", 1)[0]
    for flag in ("--warehouse", "--catalog", "--store", "--review-url", "--requester", "--policy"):
        assert f"`{flag}" in table
