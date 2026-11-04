"""MCP server: the guarded lakehouse tools, plus a training-SQL leakage check, over stdio.

    sqlguard-mcp --warehouse ./warehouse --policy policy.yaml --audit-log audit.jsonl \\
        --scope-table db.orders --spec-dir ./specs
    sqlguard-mcp --catalog prod --namespace sales --audit-log audit.jsonl --scope-table sales.orders

Every data tool of `sqlguard.safety.tools` (`list_tables`, `describe_table`, `run_sql`,
`expire_snapshots`, `remove_orphan_files`, `rollback`) is served through the GUARDED backend
only: each call goes through `Guard.submit`, so classification, policy, branch rehearsal,
invariants and the audit log apply. There is no raw backend here and no switch to get one.

No human is attached to this server, so an action the policy marks "ask" is never approved:
the client gets the decision, the reasons and the measured impact (or the maintenance plan) back
as a `needs_approval` result, and nothing is published. Approving it is an out-of-band step by an
operator (for example `Guard.submit(..., approver=...)` in their own tooling).

`check_training_sql` runs the leakage validator. Its `spec_path` must be a YAML file inside
`--spec-dir`, and `warehouse` (when given) must be the server's own warehouse, so a client cannot
make the server read or create files elsewhere. A server started with `--catalog` has no local
warehouse, so it accepts no `warehouse` argument.

Limit: the default executor rehearses a write by loading the whole table into memory and
rewriting it on a branch. That suits warehouses of benchmark size; do not point `--catalog` at a
large production table until an engine-backed executor is selectable.

Configuration comes from flags or, when a flag is absent, environment variables:

    --warehouse        SQLGUARD_WAREHOUSE        existing local Iceberg warehouse (or --catalog)
    --catalog          SQLGUARD_CATALOG          PyIceberg catalog name from ~/.pyiceberg.yaml or
                                                 PYICEBERG_CATALOG__* variables (this or
                                                 --warehouse); --audit-log is then required
    --namespace        SQLGUARD_NAMESPACE        default namespace for bare table names (db)
    --policy           SQLGUARD_POLICY           policy YAML (default: strict built-in policy)
    --audit-log        SQLGUARD_AUDIT_LOG        audit JSONL (<warehouse>/sqlguard-audit.jsonl)
    --scope-table      SQLGUARD_SCOPE_TABLES     tables the task may touch (comma-separated)
    --scope-namespace  SQLGUARD_SCOPE_NAMESPACES namespaces the task may touch (comma-separated)
    --spec-dir         SQLGUARD_SPEC_DIR         directory holding leakage specs (default: cwd)
    --max-rows         SQLGUARD_MAX_ROWS         rows a query returns to the client (50)

Without any scope flag the policy's `task_scope` applies; with neither, no table is in scope and
every write is denied. stdout is the protocol wire, so diagnostics go to stderr only.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import anyio
import anyio.to_thread
from mcp.server import Server, ServerRequestContext
from mcp.server.stdio import stdio_server
from mcp.types import (
    CallToolRequestParams,
    CallToolResult,
    ListToolsResult,
    PaginatedRequestParams,
    TextContent,
    Tool,
    ToolAnnotations,
)

from sqlguard.core.catalog import DEFAULT_NAMESPACE, LakehouseCatalog
from sqlguard.leakage.spec import LeakageSpec, SpecError
from sqlguard.leakage.validator import validate
from sqlguard.safety import ApprovalRequest, Guard, Policy, PolicyError, TaskScope
from sqlguard.safety.audit import AuditError
from sqlguard.safety.tools import (
    DEFAULT_MAX_ROWS,
    TOOL_SPECS,
    GuardedBackend,
    ToolResult,
    ToolSpec,
)

_log = logging.getLogger("sqlguard.mcp")

SERVER_NAME = "sqlguard"
LEAKAGE_TOOL = "check_training_sql"
_MAX_SPEC_BYTES = 1_000_000
_MAX_SQL_CHARS = 200_000
_SPEC_SUFFIXES = (".yaml", ".yml")

INSTRUCTIONS = (
    "Lakehouse tools guarded by sqlguard. Every query and change is classified and checked "
    "against a policy before it runs; writes are rehearsed on an Iceberg branch and published "
    "only when the policy and the data-quality checks allow it. A result with status "
    "'needs_approval' means a human reviewer must approve the action: nothing was executed. "
    "Report the reasons and impact to the user and wait; do not retry or reshape the action to "
    "get around the check. check_training_sql validates a training-set SELECT for temporal "
    "feature leakage before it is used."
)

_LEAKAGE_SPEC = ToolSpec(
    LEAKAGE_TOOL,
    "Check a training-set SELECT for temporal feature leakage (features that use information "
    "from after the prediction time). Returns a verdict (clean, leaky, unverifiable), a score "
    "and the findings. Never executes the query.",
    {
        "type": "object",
        "properties": {
            "sql": {"type": "string", "description": "The training-set SELECT to check."},
            "spec_path": {
                "type": "string",
                "description": "Leakage spec YAML, relative to the server's spec directory.",
            },
            "warehouse": {
                "type": "string",
                "description": (
                    "Optional. Must be the server's own warehouse; defaults to it. Supplies the "
                    "table schemas and, with replay, the snapshot history."
                ),
            },
            "replay": {
                "type": "boolean",
                "description": "Also replay the query point-in-time against snapshot history.",
            },
        },
        "required": ["sql", "spec_path"],
        "additionalProperties": False,
    },
)
_READ_ONLY_TOOLS = frozenset({"list_tables", "describe_table", LEAKAGE_TOOL})


class ConfigError(ValueError):
    """The server cannot start with the configuration it was given."""


@dataclass(frozen=True)
class ServerConfig:
    warehouse: Path | None = None
    catalog: str | None = None  # PyIceberg catalog name; used instead of `warehouse`
    namespace: str = DEFAULT_NAMESPACE
    policy: Path | None = None
    audit_log: Path | None = None
    scope_tables: tuple[str, ...] = ()
    scope_namespaces: tuple[str, ...] = ()
    spec_dir: Path | None = None
    max_rows: int = DEFAULT_MAX_ROWS


class _NoReviewer:
    """Approver for a server with no human attached: it never approves.

    Declining (instead of leaving the approver out) makes the guard rehearse a write on an audit
    branch first, so the client still gets the measured impact. `deferred` tells the server that
    the decline means "nobody to ask", which it reports as `needs_approval`, not as a rejection.
    """

    approver_id = "sqlguard-mcp:no-reviewer"

    def __init__(self) -> None:
        self.deferred = False

    def __call__(self, request: ApprovalRequest) -> bool:
        self.deferred = True
        return False


_NO_REVIEWER_REASON = "approval is required and no human reviewer is attached to this server"


class LakehouseTools:
    """The server's tool surface: the guarded backend plus the leakage check.

    Calls are serialized: the guard rehearses and publishes against one catalog, and two
    concurrent submissions would race on the same table pointers. Build the backend with
    `LakehouseTools.create`, which attaches the no-reviewer approver.
    """

    def __init__(
        self,
        backend: GuardedBackend,
        spec_dir: Path,
        warehouse: Path | None,
        reviewer: _NoReviewer | None = None,
    ) -> None:
        self.backend = backend
        self.spec_dir = spec_dir.resolve()
        self.warehouse = warehouse.resolve() if warehouse is not None else None
        self._reviewer = reviewer or _NoReviewer()
        self._lock = threading.Lock()

    @classmethod
    def create(
        cls,
        guard: Guard,
        scope: TaskScope | None,
        spec_dir: Path,
        warehouse: Path | None,
        max_rows: int = DEFAULT_MAX_ROWS,
    ) -> LakehouseTools:
        reviewer = _NoReviewer()
        backend = GuardedBackend(guard, scope, approver=reviewer, max_rows=max_rows)
        return cls(backend, spec_dir, warehouse, reviewer)

    def call(self, name: str, arguments: Mapping[str, Any]) -> ToolResult:
        with self._lock:
            if name == LEAKAGE_TOOL:
                return self._check_training_sql(arguments)
            self._reviewer.deferred = False
            result = self.backend.call(name, arguments)
            if result.status == "approval_rejected" and self._reviewer.deferred:
                return _awaiting_approval(result)
            return result

    # -- leakage ---------------------------------------------------------------------------------

    def _check_training_sql(self, arguments: Mapping[str, Any]) -> ToolResult:
        try:
            sql, spec_path, warehouse, replay = self._leakage_arguments(arguments)
            spec = LeakageSpec.from_yaml(self._spec_file(spec_path))
            catalog = self._leakage_catalog(warehouse)
            report = validate(sql, spec, catalog=catalog, replay=replay)
        except (ConfigError, SpecError, OSError, ValueError) as err:
            return _failure(LEAKAGE_TOOL, str(err))
        except Exception as err:  # noqa: BLE001 - tool boundary: a bad query must not end the server
            _log.warning("%s failed unexpectedly", LEAKAGE_TOOL, exc_info=True)
            return _failure(LEAKAGE_TOOL, f"{type(err).__name__}: {err}")
        return ToolResult(LEAKAGE_TOOL, "ok", {"status": "ok", **report.to_dict()})

    @staticmethod
    def _leakage_arguments(arguments: Mapping[str, Any]) -> tuple[str, str, str | None, bool]:
        allowed = set(_LEAKAGE_SPEC.parameters["properties"])
        unknown = sorted(set(arguments) - allowed)
        if unknown:
            raise ConfigError(f"unexpected argument(s): {', '.join(unknown)}")
        sql, spec_path = arguments.get("sql"), arguments.get("spec_path")
        for key, value in (("sql", sql), ("spec_path", spec_path)):
            if not isinstance(value, str) or not value.strip():
                raise ConfigError(f"{key} must be a non-empty string")
        assert isinstance(sql, str) and isinstance(spec_path, str)  # narrowed above
        if len(sql) > _MAX_SQL_CHARS:
            raise ConfigError(f"sql is longer than {_MAX_SQL_CHARS} characters")
        warehouse = arguments.get("warehouse")
        if warehouse is not None and not isinstance(warehouse, str):
            raise ConfigError("warehouse must be a string")
        replay = arguments.get("replay", False)
        if not isinstance(replay, bool):
            raise ConfigError("replay must be a boolean")
        return sql, spec_path, warehouse, replay

    def _spec_file(self, spec_path: str) -> Path:
        path = (self.spec_dir / spec_path).resolve()
        if not path.is_relative_to(self.spec_dir):
            raise ConfigError("spec_path must stay inside the server's spec directory")
        if path.suffix.lower() not in _SPEC_SUFFIXES:
            raise ConfigError("spec_path must be a .yaml or .yml file")
        if not path.is_file():
            raise ConfigError(f"no spec file {spec_path!r} in the server's spec directory")
        if path.stat().st_size > _MAX_SPEC_BYTES:
            raise ConfigError("spec file is too large")
        return path

    def _leakage_catalog(self, warehouse: str | None) -> LakehouseCatalog:
        if warehouse is not None and (
            self.warehouse is None or Path(warehouse).expanduser().resolve() != self.warehouse
        ):
            raise ConfigError("warehouse must be the server's own warehouse")
        return self.backend.catalog


def _awaiting_approval(result: ToolResult) -> ToolResult:
    """Report a decline by `_NoReviewer` as what it is: the action is held, not rejected."""
    payload = {
        **result.payload,
        "status": "needs_approval",
        "outcome": "awaiting_approval",
        "message": (
            "Not run: the action needs a human reviewer's approval and none is attached to this "
            "server. Show the reasons and impact to the user; an operator must approve it "
            "outside this session."
        ),
        "reasons": [
            _NO_REVIEWER_REASON if "approver rejected" in reason else reason
            for reason in result.payload.get("reasons", [])
        ],
    }
    guard = dict(result.guard) if result.guard else None
    if guard is not None:
        guard["outcome"] = "awaiting_approval"
        guard["reasons"] = [
            _NO_REVIEWER_REASON if "approver rejected" in reason else reason
            for reason in guard.get("reasons", [])
        ]
    return replace(result, status="needs_approval", payload=payload, guard=guard)


def _failure(name: str, message: str) -> ToolResult:
    return ToolResult(name, "error", {"status": "error", "error": message})


# -- protocol adapter ----------------------------------------------------------------------------


def _annotations(spec: ToolSpec) -> ToolAnnotations:
    read_only = spec.name in _READ_ONLY_TOOLS
    return ToolAnnotations(
        title=spec.name,
        read_only_hint=read_only,
        destructive_hint=None if read_only else True,
        open_world_hint=False,
    )


def _tools() -> list[Tool]:
    return [
        Tool(
            name=spec.name,
            description=spec.description,
            input_schema=dict(spec.parameters),
            annotations=_annotations(spec),
        )
        for spec in (*TOOL_SPECS, _LEAKAGE_SPEC)
    ]


def _to_call_result(result: ToolResult) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=result.to_message())],
        structured_content=result.to_dict(),
        is_error=result.is_error,
    )


def build_server(tools: LakehouseTools) -> Server:
    """A low-level MCP server whose tool list and schemas are exactly the ones in `tools.py`."""
    listed = _tools()

    async def list_tools(
        ctx: ServerRequestContext, params: PaginatedRequestParams | None
    ) -> ListToolsResult:
        return ListToolsResult(tools=listed)

    async def call_tool(ctx: ServerRequestContext, params: CallToolRequestParams) -> CallToolResult:
        known = {tool.name for tool in listed}
        if params.name not in known:
            return _to_call_result(
                _failure(params.name, f"unknown tool {params.name!r}; available: {sorted(known)}")
            )
        result = await anyio.to_thread.run_sync(tools.call, params.name, params.arguments or {})
        return _to_call_result(result)

    return Server(
        SERVER_NAME,
        instructions=INSTRUCTIONS,
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )


# -- configuration and entry point ---------------------------------------------------------------


def _split(value: str | None) -> tuple[str, ...]:
    return tuple(part.strip() for part in (value or "").split(",") if part.strip())


def build_config(
    argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None
) -> ServerConfig:
    env = os.environ if env is None else env
    parser = argparse.ArgumentParser(prog="sqlguard-mcp", description=__doc__.splitlines()[0])
    parser.add_argument("--warehouse")
    parser.add_argument("--catalog")
    parser.add_argument("--namespace", default=env.get("SQLGUARD_NAMESPACE", DEFAULT_NAMESPACE))
    parser.add_argument("--policy", default=env.get("SQLGUARD_POLICY"))
    parser.add_argument("--audit-log", default=env.get("SQLGUARD_AUDIT_LOG"))
    parser.add_argument("--scope-table", action="append", default=None)
    parser.add_argument("--scope-namespace", action="append", default=None)
    parser.add_argument("--spec-dir", default=env.get("SQLGUARD_SPEC_DIR"))
    parser.add_argument("--max-rows", type=int, default=env.get("SQLGUARD_MAX_ROWS"))
    args = parser.parse_args(argv)
    # a flag naming one kind of lakehouse outranks an environment variable naming the other
    warehouse = args.warehouse or (None if args.catalog else env.get("SQLGUARD_WAREHOUSE"))
    catalog = args.catalog or (None if args.warehouse else env.get("SQLGUARD_CATALOG"))
    if bool(warehouse) == bool(catalog):
        parser.error("exactly one of --warehouse (SQLGUARD_WAREHOUSE) or --catalog is required")
    return ServerConfig(
        warehouse=Path(warehouse).expanduser() if warehouse else None,
        catalog=catalog or None,
        namespace=args.namespace,
        policy=Path(args.policy).expanduser() if args.policy else None,
        audit_log=Path(args.audit_log).expanduser() if args.audit_log else None,
        scope_tables=tuple(args.scope_table or _split(env.get("SQLGUARD_SCOPE_TABLES"))),
        scope_namespaces=tuple(
            args.scope_namespace or _split(env.get("SQLGUARD_SCOPE_NAMESPACES"))
        ),
        spec_dir=Path(args.spec_dir).expanduser() if args.spec_dir else None,
        max_rows=args.max_rows or DEFAULT_MAX_ROWS,
    )


def build_tools(config: ServerConfig) -> LakehouseTools:
    """Open the lakehouse and wire policy, audit log and scope into a guarded backend."""
    warehouse = config.warehouse.resolve() if config.warehouse is not None else None
    if config.catalog is None:
        if warehouse is None:
            raise ConfigError("a warehouse or a catalog name is required")
        if not (warehouse / "catalog.db").is_file():
            raise ConfigError(f"no Iceberg warehouse at {warehouse} (catalog.db not found)")
    elif warehouse is not None:
        raise ConfigError("use either a warehouse or a catalog, not both")
    elif config.audit_log is None:
        raise ConfigError("--audit-log is required with --catalog")
    try:
        policy = Policy.load(config.policy) if config.policy else Policy()
    except (OSError, PolicyError) as err:
        raise ConfigError(f"cannot load policy: {err}") from err
    if config.catalog is not None:
        try:
            catalog = LakehouseCatalog.from_config(config.catalog, namespace=config.namespace)
        except (OSError, ValueError) as err:
            raise ConfigError(str(err)) from err
    elif warehouse is not None:
        catalog = LakehouseCatalog.local(warehouse, config.namespace)
    else:
        raise ConfigError("a warehouse or a catalog name is required")
    audit_path = config.audit_log or (warehouse / "sqlguard-audit.jsonl" if warehouse else None)
    if audit_path is None:
        raise ConfigError("--audit-log is required with --catalog")
    try:
        guard = Guard(catalog, policy, audit_path=audit_path)
    except (OSError, AuditError) as err:
        raise ConfigError(f"cannot open the audit log {audit_path}: {err}") from err
    scope = None
    if config.scope_tables or config.scope_namespaces:
        scope = TaskScope.of(
            tables=config.scope_tables,
            namespaces=config.scope_namespaces,
            default_namespace=config.namespace,
        )
    return LakehouseTools.create(
        guard, scope, config.spec_dir or Path.cwd(), warehouse, max_rows=config.max_rows
    )


async def serve(server: Server) -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main(argv: Sequence[str] | None = None) -> None:
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(name)s: %(message)s")
    config = build_config(argv)
    try:
        tools = build_tools(config)
    except ConfigError as err:
        print(f"sqlguard-mcp: {err}", file=sys.stderr)
        raise SystemExit(2) from err
    _log.info(
        "serving %s tools over stdio (%s, audit log %s)",
        len(TOOL_SPECS) + 1,
        f"catalog {config.catalog}" if config.catalog else f"warehouse {tools.warehouse}",
        tools.backend.guard.audit.path,
    )
    anyio.run(serve, build_server(tools))


if __name__ == "__main__":
    main()


__all__ = [
    "INSTRUCTIONS",
    "LEAKAGE_TOOL",
    "ConfigError",
    "LakehouseTools",
    "ServerConfig",
    "build_config",
    "build_server",
    "build_tools",
    "main",
    "serve",
]
