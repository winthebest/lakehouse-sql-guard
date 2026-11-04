"""MCP server: the guarded lakehouse tools, plus a training-SQL leakage check, over stdio.

    sqlguard-mcp --warehouse ./warehouse --policy policy.yaml --audit-log audit.jsonl \\
        --store ./actions.db --scope-table db.orders --spec-dir ./specs
    sqlguard-mcp --catalog prod --namespace sales --audit-log audit.jsonl --store ./actions.db \\
        --scope-table sales.orders

Every data tool of `sqlguard.safety.tools` (`list_tables`, `describe_table`, `run_sql`,
`expire_snapshots`, `remove_orphan_files`, `rollback`) is served through the GUARDED backend
only: each call goes through `Guard.submit`, so classification, policy, branch rehearsal,
invariants and the audit log apply. There is no raw backend here and no switch to get one.

No human is attached to this server. An action the policy marks "ask" is rehearsed on a branch,
saved in the action store (`--store`) and reported as `pending_approval` with its `action_id`, a
short summary and when it expires; nothing is published. The agent can follow it with
`get_action_status(action_id)`, which reports a state and can decide nothing: this process holds
no proof verifier and no approver, so it cannot resolve an action. A person decides it out of band
with `sqlguard serve` or `sqlguard approvals`, started with the same `--store`, `--policy` and
catalog. On startup the server settles publishes a crash left half-way (`Guard.reconcile`) and
expires actions nobody decided (`Guard.expire_pending`).

`check_training_sql` runs the leakage validator. Its `spec_path` must be a YAML file inside
`--spec-dir`, and `warehouse` (when given) must be the server's own warehouse, so a client cannot
make the server read or create files elsewhere. A server started with `--catalog` has no local
warehouse, so it accepts no `warehouse` argument.

Limit: the default executor rehearses a write by loading the whole table into memory and
rewriting it on a branch. That suits warehouses of benchmark size; do not point `--catalog` at a
large production table until an engine-backed executor is selectable.

Catalog credentials belong in the `env` of the MCP server entry of the host, never in a file the
agent can read. Configuration comes from flags or, when a flag is absent, environment variables:

    --warehouse        SQLGUARD_WAREHOUSE        existing local Iceberg warehouse (or --catalog)
    --catalog          SQLGUARD_CATALOG          PyIceberg catalog name from ~/.pyiceberg.yaml or
                                                 PYICEBERG_CATALOG__* variables (this or
                                                 --warehouse); --audit-log and --store are then
                                                 required
    --namespace        SQLGUARD_NAMESPACE        default namespace for bare table names (db)
    --policy           SQLGUARD_POLICY           policy YAML (default: strict built-in policy)
    --audit-log        SQLGUARD_AUDIT_LOG        audit JSONL (<warehouse>/sqlguard-audit.jsonl)
    --store            SQLGUARD_STORE            action store (<warehouse>/sqlguard-actions.db)
    --review-url       SQLGUARD_REVIEW_URL       scheme://host:port of the review UI (no path:
                                                 serve's link is secret), echoed to the agent
    --requester        SQLGUARD_REQUESTER        fixed label on every action this server queues
                                                 (mcp); the per-requester queue limit counts it
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
from urllib.parse import urlsplit

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
from sqlguard.safety import Guard, Policy, PolicyError, TaskScope
from sqlguard.safety.audit import AuditError
from sqlguard.safety.store import ActionStore, StoreError
from sqlguard.safety.tools import (
    DEFAULT_MAX_ROWS,
    STATUS_SPEC,
    STATUS_TOOL,
    TOOL_SPECS,
    GuardedBackend,
    ToolResult,
    ToolSpec,
    bounded_result,
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
    "'pending_approval' means a person must approve the action first: nothing was changed. "
    "Tell the user its action_id and summary, and where to review it if review_url is given, "
    "then wait; do not retry, reword or split the action to get around the check. "
    "get_action_status(action_id) shows whether it was decided; it cannot approve anything, so "
    "call it when the user asks or once after they say they decided, not in a loop. "
    "check_training_sql validates a training-set SELECT for temporal feature leakage before it "
    "is used."
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
_READ_ONLY_TOOLS = frozenset({"list_tables", "describe_table", STATUS_TOOL, LEAKAGE_TOOL})
DEFAULT_REQUESTER = "mcp"
_MAX_REQUESTER_CHARS = 64


class ConfigError(ValueError):
    """The server cannot start with the configuration it was given."""


@dataclass(frozen=True)
class ServerConfig:
    warehouse: Path | None = None
    catalog: str | None = None  # PyIceberg catalog name; used instead of `warehouse`
    namespace: str = DEFAULT_NAMESPACE
    policy: Path | None = None
    audit_log: Path | None = None
    store: Path | None = None  # action store; default <warehouse>/sqlguard-actions.db
    review_url: str | None = None
    requester: str = DEFAULT_REQUESTER
    scope_tables: tuple[str, ...] = ()
    scope_namespaces: tuple[str, ...] = ()
    spec_dir: Path | None = None
    max_rows: int = DEFAULT_MAX_ROWS


class LakehouseTools:
    """The server's tool surface: the guarded backend plus the leakage check.

    Calls are serialized: the guard rehearses and publishes against one catalog, and two
    concurrent submissions would race on the same table pointers. Build it with
    `LakehouseTools.create`. The backend has no approver, so an action that needs one is queued
    in the Guard's store and never decided here.
    """

    def __init__(
        self,
        backend: GuardedBackend,
        spec_dir: Path,
        warehouse: Path | None,
        review_url: str | None = None,
    ) -> None:
        self.backend = backend
        self.spec_dir = spec_dir.resolve()
        self.warehouse = warehouse.resolve() if warehouse is not None else None
        self.review_url = review_url
        self._lock = threading.Lock()

    @classmethod
    def create(
        cls,
        guard: Guard,
        scope: TaskScope | None,
        spec_dir: Path,
        warehouse: Path | None,
        max_rows: int = DEFAULT_MAX_ROWS,
        review_url: str | None = None,
    ) -> LakehouseTools:
        backend = GuardedBackend(guard, scope, approver=None, max_rows=max_rows)
        return cls(backend, spec_dir, warehouse, review_url)

    @property
    def tool_specs(self) -> tuple[ToolSpec, ...]:
        status = (STATUS_SPEC,) if self.backend.supports_status else ()
        return (*TOOL_SPECS, *status, _LEAKAGE_SPEC)

    def call(self, name: str, arguments: Mapping[str, Any]) -> ToolResult:
        with self._lock:
            if name == LEAKAGE_TOOL:
                return bounded_result(self._check_training_sql(arguments))
            result = self.backend.call(name, arguments)
            if result.status == "pending_approval" and self.review_url:
                result = replace(result, payload={**result.payload, "review_url": self.review_url})
            return bounded_result(result)

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


def _tools(specs: Sequence[ToolSpec]) -> list[Tool]:
    return [
        Tool(
            name=spec.name,
            description=spec.description,
            input_schema=dict(spec.parameters),
            annotations=_annotations(spec),
        )
        for spec in specs
    ]


def _to_call_result(result: ToolResult) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=result.to_message())],
        structured_content=result.to_dict(),
        is_error=result.is_error,
    )


def build_server(tools: LakehouseTools) -> Server:
    """A low-level MCP server whose tool list and schemas are exactly the ones in `tools.py`."""
    listed = _tools(tools.tool_specs)

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


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _review_url(value: str | None) -> str | None:
    """The address of the review UI, as scheme://host[:port] and nothing else.

    `sqlguard serve` puts the UI under a secret path and prints a link that carries it. That path
    is what stops a program on another local port from using the person's session cookie, so it
    must never reach the agent: this accepts no path, query or fragment.
    """
    if not value:
        return None
    text = value.strip()
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError as err:
        raise ConfigError(f"--review-url is not a valid URL: {err}") from err
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ConfigError("--review-url must be an http(s) URL such as http://127.0.0.1:8765")
    if parts.hostname.lower() not in _LOOPBACK_HOSTS:
        raise ConfigError("--review-url must point at this machine (127.0.0.1, localhost or ::1)")
    if (
        parts.path not in ("", "/")
        or parts.query
        or parts.fragment
        or parts.username
        or parts.password
    ):
        raise ConfigError(
            "--review-url must be the server address only (scheme://host:port); never paste "
            "the link that `sqlguard serve` prints, its path is a secret"
        )
    netloc = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    return f"{parts.scheme}://{netloc}" + (f":{port}" if port is not None else "")


def _requester(value: str) -> str:
    label = value.strip()
    if not label or len(label) > _MAX_REQUESTER_CHARS or not label.isprintable():
        raise ConfigError(f"--requester must be 1 to {_MAX_REQUESTER_CHARS} printable characters")
    return label


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
    parser.add_argument("--store", default=env.get("SQLGUARD_STORE"))
    parser.add_argument("--review-url", default=env.get("SQLGUARD_REVIEW_URL"))
    parser.add_argument("--requester", default=env.get("SQLGUARD_REQUESTER") or DEFAULT_REQUESTER)
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
    try:
        review_url = _review_url(args.review_url)
        requester = _requester(args.requester)
    except ConfigError as err:
        parser.error(str(err))
    return ServerConfig(
        warehouse=Path(warehouse).expanduser() if warehouse else None,
        catalog=catalog or None,
        namespace=args.namespace,
        policy=Path(args.policy).expanduser() if args.policy else None,
        audit_log=Path(args.audit_log).expanduser() if args.audit_log else None,
        store=Path(args.store).expanduser() if args.store else None,
        review_url=review_url,
        requester=requester,
        scope_tables=tuple(args.scope_table or _split(env.get("SQLGUARD_SCOPE_TABLES"))),
        scope_namespaces=tuple(
            args.scope_namespace or _split(env.get("SQLGUARD_SCOPE_NAMESPACES"))
        ),
        spec_dir=Path(args.spec_dir).expanduser() if args.spec_dir else None,
        max_rows=args.max_rows or DEFAULT_MAX_ROWS,
    )


def build_tools(config: ServerConfig) -> LakehouseTools:
    """Open the lakehouse and wire policy, audit log, store and scope into a guarded backend.

    The Guard gets the action store and a fixed requester label, and neither an approver nor a
    proof verifier: this process can queue an action and read its status, never decide it.
    """
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
    store_path = config.store or (warehouse / "sqlguard-actions.db" if warehouse else None)
    if audit_path is None:
        raise ConfigError("--audit-log is required with --catalog")
    if store_path is None:
        raise ConfigError("--store is required with --catalog")
    try:
        store = ActionStore(store_path)
    except StoreError as err:
        raise ConfigError(str(err)) from err
    try:
        guard = Guard(
            catalog, policy, audit_path=audit_path, store=store, requester=config.requester
        )
    except (OSError, AuditError) as err:
        raise ConfigError(f"cannot open the audit log {audit_path}: {err}") from err
    _settle(guard)
    scope = None
    if config.scope_tables or config.scope_namespaces:
        scope = TaskScope.of(
            tables=config.scope_tables,
            namespaces=config.scope_namespaces,
            default_namespace=config.namespace,
        )
    return LakehouseTools.create(
        guard,
        scope,
        config.spec_dir or Path.cwd(),
        warehouse,
        max_rows=config.max_rows,
        review_url=config.review_url,
    )


def _settle(guard: Guard) -> None:
    """Settle what a previous run left behind: half-finished publishes, then late actions.

    A failure is logged, not fatal: the tools still work, and `sqlguard serve` repeats this on
    its own schedule.
    """
    try:
        for settled in guard.reconcile():
            _log.warning(
                "settled action %s as %s: %s", settled.action_id, settled.state, settled.reason
            )
        for action_id in guard.expire_pending():
            _log.info("expired action %s", action_id)
    except Exception:  # noqa: BLE001 - startup housekeeping must not keep the tools from serving
        _log.warning("could not settle earlier actions; serving anyway", exc_info=True)


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
        len(tools.tool_specs),
        f"catalog {config.catalog}" if config.catalog else f"warehouse {tools.warehouse}",
        tools.backend.guard.audit.path,
    )
    anyio.run(serve, build_server(tools))


if __name__ == "__main__":
    main()


__all__ = [
    "INSTRUCTIONS",
    "LEAKAGE_TOOL",
    "DEFAULT_REQUESTER",
    "ConfigError",
    "LakehouseTools",
    "ServerConfig",
    "build_config",
    "build_server",
    "build_tools",
    "main",
    "serve",
]
