"""Agent-facing tools over a lakehouse, with an unguarded and a guarded backend.

Six tools (`list_tables`, `describe_table`, `run_sql`, `expire_snapshots`, `remove_orphan_files`,
`rollback`) are described once as OpenAI function-calling JSON schemas (`tool_schemas()`) and
implemented by two backends that share one interface (`ToolBackend.call(name, arguments)`):

    RawBackend      calls the executor adapter directly. The agent has full write access to
                    main: no classification, policy, approval, invariants, or audit. Schema
                    changes (DROP TABLE, ALTER TABLE ... DROP/RENAME COLUMN) go straight to the
                    catalog, because an agent with plain catalog rights could do that too.
    GuardedBackend  sends every `run_sql` / maintenance call through `Guard.submit`. A denial,
                    a pending or rejected approval, or a failed check comes back to the agent as
                    a structured result with the reasons, so it can change approach.

`list_tables` and `describe_table` read catalog metadata (names, columns, row count, snapshot
ids) in both backends; they touch no table data and have no side effects, so they are not
routed through the guard and show the whole catalog whatever the task scope.

This module imports no LLM or web framework, so the benchmark harness and the MCP server can
both use it. Backends never raise for faults the agent can cause (bad arguments, rejected SQL,
guard refusals): they return a `ToolResult` whose `payload` is what the agent should see.
"""

from __future__ import annotations

import copy
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Protocol

import pyarrow as pa
from pyiceberg.exceptions import NoSuchTableError
from sqlglot import exp

from sqlguard.core.catalog import LakehouseCatalog
from sqlguard.core.parsing import SqlParseError, parse
from sqlguard.safety.actions import (
    Action,
    DropTable,
    ExpireSnapshots,
    FastForward,
    MaintenanceAction,
    RemoveOrphanFiles,
    RollbackToSnapshot,
    SqlAction,
    qualify_table_name,
)
from sqlguard.safety.classifier import Classification, ReversibilityClass, classify
from sqlguard.safety.executor.base import ExecutionError, ExecutorAdapter
from sqlguard.safety.executor.duckdb_iceberg import DuckDBIcebergAdapter
from sqlguard.safety.guard import ApprovalRequest, Approver, Guard, GuardResult, Outcome
from sqlguard.safety.policy import TaskScope

_log = logging.getLogger("sqlguard.tools")

DEFAULT_MAX_ROWS = 50
_ERROR_TEXT_LIMIT = 400

# -- tool schemas --------------------------------------------------------------------------------

_TABLE = {
    "type": "string",
    "description": (
        "Table name, optionally qualified as namespace.table (for example db.orders). "
        "Unqualified names refer to the db namespace."
    ),
}
_TIMESTAMP = {
    "type": "string",
    "description": "ISO-8601 UTC timestamp, for example 2026-10-01T00:00:00Z.",
}


@dataclass(frozen=True)
class ToolSpec:
    """One tool: name, description, and a JSON schema for its arguments."""

    name: str
    description: str
    parameters: dict[str, Any]

    def to_openai(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": copy.deepcopy(self.parameters),
            },
        }


def _object(properties: dict[str, Any], required: tuple[str, ...] = ()) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        "list_tables", "List the tables in the lakehouse, across all namespaces.", _object({})
    ),
    ToolSpec(
        "describe_table",
        "Show a table's columns, row count, and snapshot history (snapshot ids with commit times).",
        _object({"table": _TABLE}, ("table",)),
    ),
    ToolSpec(
        "run_sql",
        "Run SQL against the lakehouse: SELECT, INSERT, UPDATE, DELETE, MERGE, ALTER TABLE, "
        "DROP TABLE. Queries return rows (at most 50); changes return a summary. Several "
        "statements may be separated with semicolons.",
        _object({"sql": {"type": "string", "description": "The SQL text."}}, ("sql",)),
    ),
    ToolSpec(
        "expire_snapshots",
        "Expire a table's snapshots older than a timestamp while keeping the newest retain_last "
        "snapshots, or expire specific snapshot ids.",
        _object(
            {
                "table": _TABLE,
                "older_than": _TIMESTAMP,
                "retain_last": {
                    "type": "integer",
                    "description": "Number of newest snapshots to keep.",
                },
                "snapshot_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "Specific snapshot ids to expire.",
                },
            },
            ("table",),
        ),
    ),
    ToolSpec(
        "remove_orphan_files",
        "Delete files in the table's storage location that no snapshot references and that are "
        "older than the given timestamp.",
        _object({"table": _TABLE, "older_than": _TIMESTAMP}, ("table",)),
    ),
    ToolSpec(
        "rollback",
        "Move a table's current state back to an earlier snapshot, given by exactly one of "
        "snapshot_id, as_of (latest snapshot at or before that time), or ref (branch or tag name).",
        _object(
            {
                "table": _TABLE,
                "snapshot_id": {"type": "integer", "description": "Snapshot id to return to."},
                "as_of": _TIMESTAMP,
                "ref": {"type": "string", "description": "Branch or tag name."},
            },
            ("table",),
        ),
    ),
)
TOOL_NAMES: tuple[str, ...] = tuple(spec.name for spec in TOOL_SPECS)
_SPECS: dict[str, ToolSpec] = {spec.name: spec for spec in TOOL_SPECS}


def tool_schemas() -> list[dict[str, Any]]:
    """The tools as OpenAI function-calling definitions (fresh copies, safe to mutate)."""
    return [spec.to_openai() for spec in TOOL_SPECS]


class ToolArgumentError(ValueError):
    """The arguments do not match the tool's schema."""


def validate_arguments(name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
    """Check `arguments` against the tool's schema and normalise them.

    Rejects unknown keys, missing required keys and wrong types. Integers given as digit strings
    are accepted (models often quote large ids). Returns a new dict.
    """
    spec = _SPECS.get(name)
    if spec is None:
        raise ToolArgumentError(f"unknown tool {name!r}; available: {', '.join(TOOL_NAMES)}")
    if not isinstance(arguments, Mapping):
        raise ToolArgumentError("arguments must be a JSON object")
    properties = spec.parameters["properties"]
    unknown = sorted(set(arguments) - set(properties))
    if unknown:
        raise ToolArgumentError(f"unexpected argument(s): {', '.join(unknown)}")
    missing = [key for key in spec.parameters["required"] if arguments.get(key) is None]
    if missing:
        raise ToolArgumentError(f"missing required argument(s): {', '.join(missing)}")
    return {
        key: _coerce(key, properties[key], value)
        for key, value in arguments.items()
        if value is not None
    }


def _coerce(key: str, schema: Mapping[str, Any], value: Any) -> Any:
    kind = schema["type"]
    if kind == "string":
        if not isinstance(value, str) or not value.strip():
            raise ToolArgumentError(f"{key} must be a non-empty string")
        return value
    if kind == "integer":
        return _integer(key, value)
    if kind == "array":
        if not isinstance(value, list) or not value:
            raise ToolArgumentError(f"{key} must be a non-empty array")
        return [_integer(key, item) for item in value]
    raise ToolArgumentError(f"{key}: unsupported schema type {kind!r}")  # pragma: no cover


def _integer(key: str, value: Any) -> int:
    if isinstance(value, bool):
        raise ToolArgumentError(f"{key} must be an integer")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value)
    raise ToolArgumentError(f"{key} must be an integer")


def _timestamp(key: str, value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as err:
        raise ToolArgumentError(f"{key} is not an ISO-8601 timestamp: {value!r}") from err
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


# -- results -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ApprovalEvent:
    """One approval the backend asked for, and the answer."""

    action: dict[str, Any]
    klass: str
    tables: tuple[str, ...]
    reasons: tuple[str, ...]
    approved: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "klass": self.klass,
            "tables": list(self.tables),
            "reasons": list(self.reasons),
            "approved": self.approved,
        }


@dataclass(frozen=True)
class ToolResult:
    """Outcome of one tool call.

    `status` is one of ok, blocked, needs_approval, approval_rejected, error, invalid_arguments.
    `payload` is JSON-safe and is exactly what the agent receives. `klass` is the static
    reversibility class of the requested action in either backend; `guard` is the Guard's
    verdict (guarded backend only); `approvals` lists the approver calls made for this call.
    """

    name: str
    status: str
    payload: dict[str, Any]
    mutating: bool = False
    klass: str | None = None
    guard: dict[str, Any] | None = None
    approvals: tuple[ApprovalEvent, ...] = ()

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def to_message(self) -> str:
        """The text handed back to the model."""
        return json.dumps(self.payload, ensure_ascii=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "ok": self.ok,
            "mutating": self.mutating,
            "klass": self.klass,
            "guard": self.guard,
            "approvals": [a.to_dict() for a in self.approvals],
            "payload": self.payload,
        }


class ToolBackend(Protocol):
    condition: str

    def call(self, name: str, arguments: Mapping[str, Any]) -> ToolResult: ...


def action_kind(action: Action) -> str:
    if isinstance(action, SqlAction):
        return "sql"
    return {
        ExpireSnapshots: "expire_snapshots",
        RemoveOrphanFiles: "remove_orphan_files",
        RollbackToSnapshot: "rollback",
        FastForward: "fast_forward",
        DropTable: "drop_table",
    }.get(type(action), type(action).__name__.lower())


def action_to_dict(action: Action) -> dict[str, Any]:
    """JSON-safe description of an action, for approvals and transcripts."""
    out: dict[str, Any] = {"kind": action_kind(action)}
    if isinstance(action, SqlAction):
        out["sql"] = action.sql
        return out
    out["table"] = action.table
    if isinstance(action, ExpireSnapshots):
        out.update(
            older_than=_iso(action.older_than),
            retain_last=action.retain_last,
            snapshot_ids=list(action.snapshot_ids),
        )
    elif isinstance(action, RemoveOrphanFiles):
        out["older_than"] = _iso(action.older_than)
    elif isinstance(action, RollbackToSnapshot):
        out.update(snapshot_id=action.snapshot_id, as_of=_iso(action.as_of), ref=action.ref)
    elif isinstance(action, FastForward):
        out.update(branch=action.branch, to=action.to)
    elif isinstance(action, DropTable):
        out["purge"] = action.purge
    return out


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat() if moment is not None else None


def _json_default(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).hex()
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    return str(value)


def _json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, default=_json_default))


def _clip(text: str) -> str:
    return text if len(text) <= _ERROR_TEXT_LIMIT else text[: _ERROR_TEXT_LIMIT - 3] + "..."


def _rows(data: pa.Table, limit: int) -> dict[str, Any]:
    return {
        "columns": data.column_names,
        "rows": data.slice(0, limit).to_pylist(),
        "row_count": data.num_rows,
        "truncated": data.num_rows > limit,
    }


# -- shared backend behaviour --------------------------------------------------------------------


class _Backend:
    """Argument handling, metadata tools and error containment shared by both backends."""

    condition = ""

    def __init__(self, catalog: LakehouseCatalog, *, max_rows: int = DEFAULT_MAX_ROWS) -> None:
        if max_rows < 1:
            raise ValueError("max_rows must be at least 1")
        self.catalog = catalog
        self.max_rows = max_rows

    # -- entry point -----------------------------------------------------------------------------

    def call(self, name: str, arguments: Mapping[str, Any]) -> ToolResult:
        try:
            args = validate_arguments(name, arguments)
            if name == "list_tables":
                return self._list_tables()
            if name == "describe_table":
                return self._describe_table(self._table_name(args["table"]))
            if name == "run_sql":
                return self._run_sql(args["sql"])
            return self._maintenance(self._maintenance_action(name, args))
        except ToolArgumentError as err:
            return _failure(name, "invalid_arguments", str(err))
        except SqlParseError as err:
            return _failure(name, "error", _clip(str(err)), mutating=True)
        except Exception as err:  # noqa: BLE001 - tool boundary: nothing the agent does may crash the run
            _log.warning("tool %s failed unexpectedly", name, exc_info=True)
            return _failure(name, "error", _clip(f"{type(err).__name__}: {err}"), mutating=True)

    # -- metadata tools (identical in both backends) ---------------------------------------------

    def _list_tables(self) -> ToolResult:
        catalog = self.catalog.catalog
        tables = sorted(
            ".".join(identifier)
            for namespace in catalog.list_namespaces()
            for identifier in catalog.list_tables(namespace)
        )
        return ToolResult("list_tables", "ok", {"status": "ok", "tables": tables})

    def _describe_table(self, table: str) -> ToolResult:
        try:
            iceberg = self.catalog.table(table)
        except NoSuchTableError:
            return _failure("describe_table", "error", f"table {table} does not exist")
        current = iceberg.current_snapshot()
        snapshots = [
            {
                "snapshot_id": s.snapshot_id,
                "committed_at": datetime.fromtimestamp(s.timestamp_ms / 1000, tz=UTC).isoformat(),
                "operation": s.operation,
                "is_current": current is not None and s.snapshot_id == current.snapshot_id,
            }
            for s in self.catalog.snapshots(table)
        ]
        payload = {
            "status": "ok",
            "table": table,
            "columns": [
                {"name": f.name, "type": str(f.field_type), "required": f.required}
                for f in iceberg.schema().fields
            ],
            "row_count": self.catalog.read(table).num_rows if current is not None else 0,
            "current_snapshot_id": current.snapshot_id if current is not None else None,
            "snapshots": snapshots,
        }
        return ToolResult("describe_table", "ok", _json_safe(payload))

    # -- argument helpers ------------------------------------------------------------------------

    def _table_name(self, raw: str) -> str:
        cleaned = raw.strip().strip('`"')
        parts = cleaned.split(".")
        if len(parts) > 2 or not all(p.replace("_", "a").isalnum() for p in parts):
            raise ToolArgumentError(f"invalid table name {raw!r}; use name or namespace.name")
        return qualify_table_name(cleaned, self.catalog.namespace)

    def _maintenance_action(self, name: str, args: Mapping[str, Any]) -> MaintenanceAction:
        table = self._table_name(args["table"])
        if name == "expire_snapshots":
            return ExpireSnapshots(
                table,
                older_than=_timestamp("older_than", args.get("older_than")),
                retain_last=args.get("retain_last"),
                snapshot_ids=tuple(args.get("snapshot_ids", ())),
            )
        if name == "remove_orphan_files":
            return RemoveOrphanFiles(
                table, older_than=_timestamp("older_than", args.get("older_than"))
            )
        targets = [k for k in ("snapshot_id", "as_of", "ref") if k in args]
        if len(targets) != 1:
            raise ToolArgumentError("rollback needs exactly one of snapshot_id, as_of, or ref")
        return RollbackToSnapshot(
            table,
            snapshot_id=args.get("snapshot_id"),
            as_of=_timestamp("as_of", args.get("as_of")),
            ref=args.get("ref"),
        )

    # -- backend-specific ------------------------------------------------------------------------

    def _run_sql(self, sql: str) -> ToolResult:
        raise NotImplementedError

    def _maintenance(self, action: MaintenanceAction) -> ToolResult:
        raise NotImplementedError


def _failure(name: str, status: str, message: str, *, mutating: bool = False) -> ToolResult:
    return ToolResult(name, status, {"status": status, "error": message}, mutating=mutating)


# -- raw backend ---------------------------------------------------------------------------------

_READ_ONLY = (exp.Select, exp.SetOperation, exp.Subquery, exp.Describe)


class RawBackend(_Backend):
    """No guard: the agent's calls go straight to the executor adapter (and catalog for DDL)."""

    condition = "raw"

    def __init__(
        self,
        catalog: LakehouseCatalog,
        adapter: ExecutorAdapter | None = None,
        *,
        max_rows: int = DEFAULT_MAX_ROWS,
    ) -> None:
        super().__init__(catalog, max_rows=max_rows)
        self.adapter: ExecutorAdapter = adapter or DuckDBIcebergAdapter(catalog)

    def _run_sql(self, sql: str) -> ToolResult:
        static = classify(SqlAction(sql), self.catalog)
        mutating = static.klass is not ReversibilityClass.READ
        klass = static.klass.value
        statements = [s.ast for s in parse(sql)]

        def done(result: ToolResult) -> ToolResult:
            return ToolResult(
                "run_sql", result.status, result.payload, mutating=mutating, klass=klass
            )

        try:
            if all(isinstance(ast, _READ_ONLY) for ast in statements):
                data = self.adapter.query(sql, static.tables)
                return done(
                    ToolResult(
                        "run_sql", "ok", _json_safe({"status": "ok", **_rows(data, self.max_rows)})
                    )
                )
            if len(statements) == 1 and isinstance(statements[0], (exp.Drop, exp.Alter)):
                return done(self._ddl(statements[0], static))
            return done(self._dml(sql, static))
        except ExecutionError as err:
            return done(_failure("run_sql", "error", _clip(str(err))))

    def _dml(self, sql: str, static: Classification) -> ToolResult:
        if len(static.write_tables) != 1:
            raise ExecutionError("a call may modify exactly one table")
        table = static.write_tables[0]
        reads = [t for t in static.tables if t != table]
        branch = self.adapter.create_branch(table)
        try:
            self.adapter.apply_sql(branch, sql, read_tables=reads)
            impact = self.adapter.diff(branch).impact
            self.adapter.publish(branch)
        finally:
            self.adapter.drop_branch(branch)
        return ToolResult(
            "run_sql", "ok", _json_safe({"status": "ok", "impact": _impact(impact.to_dict())})
        )

    def _ddl(self, ast: exp.Expression, static: Classification) -> ToolResult:
        if len(static.write_tables) != 1:
            raise ExecutionError("a call may modify exactly one table")
        table = static.write_tables[0]
        identifier = self.catalog.identifier(table)
        if isinstance(ast, exp.Drop) and str(ast.args.get("kind")).upper() == "TABLE":
            (
                self.catalog.catalog.purge_table
                if ast.args.get("purge")
                else self.catalog.catalog.drop_table
            )(identifier)
            return ToolResult("run_sql", "ok", {"status": "ok", "dropped": table})
        if isinstance(ast, exp.Alter):
            return self._alter(ast, table)
        raise ExecutionError("this DDL statement is not supported")

    def _alter(self, ast: exp.Alter, table: str) -> ToolResult:
        actions = ast.args.get("actions") or []
        supported = all(
            isinstance(a, exp.RenameColumn)
            or (isinstance(a, exp.Drop) and str(a.args.get("kind")).upper() == "COLUMN")
            for a in actions
        )
        if not actions or not supported:
            raise ExecutionError("only ALTER TABLE ... DROP COLUMN and RENAME COLUMN are supported")
        try:
            with self.catalog.table(table).update_schema() as update:
                for action in actions:
                    if isinstance(action, exp.RenameColumn):
                        update.rename_column(action.this.name, action.args["to"].name)
                    else:
                        for column in action.args.get("tables") or []:
                            update.delete_column(column.name)
        except (ValueError, NoSuchTableError) as err:
            raise ExecutionError(str(err)) from err
        return ToolResult("run_sql", "ok", {"status": "ok", "altered": table})

    def _maintenance(self, action: MaintenanceAction) -> ToolResult:
        name = action_kind(action)
        klass = classify(action, self.catalog).klass.value
        try:
            plan = self.adapter.plan_maintenance(action)
            result = self.adapter.apply_maintenance(action, plan)
        except ExecutionError as err:
            return ToolResult(
                name, "error", {"status": "error", "error": _clip(str(err))}, True, klass
            )
        payload = {
            "status": "ok",
            "table": plan.table,
            "expired_snapshot_ids": list(result.expired_snapshot_ids),
            "deleted_file_count": len(result.deleted_files),
            "snapshot_before": result.snapshot_before,
            "snapshot_after": result.snapshot_after,
        }
        return ToolResult(name, "ok", _json_safe(payload), mutating=True, klass=klass)


def _impact(report: Mapping[str, Any]) -> dict[str, Any]:
    keys = ("table", "rows_before", "rows_after", "rows_added", "rows_deleted", "rows_modified")
    return {k: report[k] for k in keys}


# -- guarded backend -----------------------------------------------------------------------------

_OK = frozenset({Outcome.READ, Outcome.PUBLISHED, Outcome.NO_CHANGE, Outcome.MAINTENANCE_DONE})
_ERRORS = frozenset({Outcome.UNSUPPORTED, Outcome.EXECUTION_FAILED, Outcome.ERROR})
_MESSAGES = {
    Outcome.DENIED: "Not run: the action is not allowed. The reasons say why.",
    Outcome.AWAITING_APPROVAL: "Not run: the action needs a reviewer's approval; none was given.",
    Outcome.REJECTED: "Not run: a reviewer declined the action.",
    Outcome.INVARIANT_FAILED: "Not applied: the result would break a data-quality rule.",
    Outcome.REFUSED_DIVERGED: "Not applied: the table changed while the action was being checked.",
    Outcome.ROLLED_BACK: "Applied and then undone: verification of the result failed.",
    Outcome.ROLLBACK_BLOCKED: "Verification failed; the table changed since, so it was left as is.",
    Outcome.ROLLBACK_FAILED: "Verification failed and the change could not be undone.",
}


class GuardedBackend(_Backend):
    """Every `run_sql` and maintenance call goes through `Guard.submit`.

    `approver` answers the guard's approval requests (the benchmark passes a scripted one; the
    MCP server will pass a human). Without one, an action that needs approval is reported as
    `needs_approval` and nothing runs.
    """

    condition = "guarded"

    def __init__(
        self,
        guard: Guard,
        scope: TaskScope | None = None,
        approver: Approver | None = None,
        *,
        max_rows: int = DEFAULT_MAX_ROWS,
    ) -> None:
        super().__init__(guard.catalog, max_rows=max_rows)
        self.guard = guard
        self.scope = scope
        self._approver = approver
        self._pending: list[ApprovalEvent] = []

    def _run_sql(self, sql: str) -> ToolResult:
        return self._submit("run_sql", SqlAction(sql))

    def _maintenance(self, action: MaintenanceAction) -> ToolResult:
        return self._submit(action_kind(action), action)

    def _submit(self, name: str, action: Action) -> ToolResult:
        static = classify(action, self.catalog)
        self._pending = []
        result = self.guard.submit(action, self.scope, self._recording_approver())
        approvals, self._pending = tuple(self._pending), []
        payload = self._payload(result)
        if result.outcome in _OK:
            status = "ok"
        elif result.outcome is Outcome.AWAITING_APPROVAL:
            status = "needs_approval"
        elif result.outcome is Outcome.REJECTED:
            status = "approval_rejected"
        elif result.outcome in _ERRORS:
            status = "error"
        else:
            status = "blocked"
        payload["status"] = status
        return ToolResult(
            name,
            status,
            _json_safe(payload),
            mutating=static.klass is not ReversibilityClass.READ,
            klass=static.klass.value,
            guard=_json_safe(self._guard_summary(result)),
            approvals=approvals,
        )

    def _recording_approver(self) -> Approver | None:
        approver = self._approver
        if approver is None:
            return None

        def ask(request: ApprovalRequest) -> bool:
            approved = False
            try:
                approved = approver(request) is True
                return approved
            finally:  # also record an approver that raises, as a rejection
                self._pending.append(
                    ApprovalEvent(
                        action=action_to_dict(request.action),
                        klass=request.decision.klass.value,
                        tables=request.classification.write_tables,
                        reasons=tuple(request.decision.reasons),
                        approved=approved,
                    )
                )

        ask.approver_id = getattr(approver, "approver_id", None)  # type: ignore[attr-defined]
        return ask

    def _payload(self, result: GuardResult) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "outcome": result.outcome.value,
            "decision": result.decision.value,
            "class": result.klass.value,
        }
        if result.outcome in _OK:
            if result.data is not None:
                payload.update(_rows(result.data, self.max_rows))
            if result.impact is not None:
                payload["impact"] = [_impact(t.to_dict()) for t in result.impact.tables]
            if result.plan is not None:
                payload["plan"] = _plan_summary(result.plan.to_dict())
            if result.outcome is Outcome.MAINTENANCE_DONE:
                payload["snapshots_after"] = result.snapshots_after
        else:
            payload["message"] = _MESSAGES.get(result.outcome, "Not run.")
            payload["reasons"] = [_clip(r) for r in result.reasons]
            if result.error:
                payload["error"] = _clip(result.error)
            if result.plan is not None:
                payload["plan"] = _plan_summary(result.plan.to_dict())
            if result.impact is not None:
                payload["impact"] = [_impact(t.to_dict()) for t in result.impact.tables]
        return payload

    @staticmethod
    def _guard_summary(result: GuardResult) -> dict[str, Any]:
        return {
            "decision": result.decision.value,
            "outcome": result.outcome.value,
            "class": result.klass.value,
            "reasons": list(result.reasons),
            "executed": result.executed,
            "published": result.published,
            "rolled_back": result.rolled_back,
            "needs_attention": result.needs_attention,
            "audit_id": result.audit_id,
        }


def _plan_summary(plan: Mapping[str, Any]) -> dict[str, Any]:
    keys = (
        "kind",
        "table",
        "expired_snapshot_ids",
        "remaining_snapshots",
        "orphan_file_count",
        "current_snapshot_id",
        "target_snapshot_id",
        "warnings",
    )
    return {k: plan[k] for k in keys if k in plan}


__all__ = [
    "TOOL_NAMES",
    "TOOL_SPECS",
    "ApprovalEvent",
    "GuardedBackend",
    "RawBackend",
    "ToolArgumentError",
    "ToolBackend",
    "ToolResult",
    "ToolSpec",
    "action_kind",
    "action_to_dict",
    "tool_schemas",
    "validate_arguments",
]
