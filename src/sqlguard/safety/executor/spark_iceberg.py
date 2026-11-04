"""Executor adapter v2: Apache Spark with the Iceberg SQL extensions.

Where `DuckDBIcebergAdapter` rewrites whole tables in process, this adapter hands the work to a
Spark session, so it suits tables that do not fit in memory:

  * `create_branch` / `drop_branch` run `ALTER TABLE ... CREATE BRANCH / DROP BRANCH`.
  * `apply_sql` runs the agent's DML against the branch only. The target table is rewritten to
    the branch identifier `catalog.ns.table.branch_<name>`, so the statement physically cannot
    touch main. Every other table it reads is qualified to the Spark catalog; a table the caller
    did not clear for this statement is refused.
  * `publish` is the Iceberg procedure `fast_forward`: it moves main to the branch head and
    fails when main is no longer an ancestor of the branch, which is exactly the "main moved
    since the branch was cut" case (`BranchDiverged`).
  * `rollback` / `fast_forward` / `expire_snapshots` / `remove_orphan_files` are the matching
    `CALL <catalog>.system.<procedure>` statements. Orphan cleanup is planned with the
    procedure's `dry_run`, and the real run is refused if it would delete anything the approved
    plan did not list.

Table metadata (snapshots, refs, schemas, row data for the impact report) is read through the
PyIceberg catalog the Guard already holds, so both sides must point at the same Iceberg catalog.
Planning of snapshot expiry, rollback and fast-forward is metadata-only and is delegated to
`DuckDBIcebergAdapter.plan_maintenance`; no DuckDB runs.

`spark` is any object with `sql(text) -> DataFrame` and a catalog named `spark_catalog`
configured with `IcebergSparkSessionExtensions`: a classic local `SparkSession` or a Spark
Connect session. `pyspark` is an optional dependency (extra `spark`) and is never imported here.

Differences from the DuckDB adapter that an approver should know:
  * `expire_snapshots` deletes the data and manifest files that no remaining snapshot needs, not
    just the metadata (the plan says so).
  * `rollback` has no compare-and-swap in Spark. It re-checks main right before the call and,
    if a writer slipped in between, restores the writer's snapshot and raises `BranchDiverged`.
  * Spark reports orphan files by location only, so `OrphanFile.modified` is the cutoff the
    procedure applied (every listed file is older than it).
  * Timestamps are collected in the Python process's local zone; results are converted to UTC.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, Protocol

import pyarrow as pa
from pyiceberg.exceptions import NoSuchTableError
from pyiceberg.table import Table
from sqlglot import exp

from sqlguard.core.catalog import LakehouseCatalog, SnapshotInfo
from sqlguard.core.parsing import DEFAULT_DIALECT, Explain, SqlParseError, parse
from sqlguard.safety.actions import (
    FastForward,
    MaintenanceAction,
    RemoveOrphanFiles,
    RollbackToSnapshot,
)
from sqlguard.safety.executor.base import (
    BranchDiff,
    BranchDiverged,
    BranchHandle,
    ExecutionError,
    MaintenancePlan,
    MaintenanceResult,
    PublishResult,
    UnsupportedAction,
)
from sqlguard.safety.executor.duckdb_iceberg import (
    AUDIT_BRANCH_PREFIX,
    MAIN,
    DuckDBIcebergAdapter,
    read_only_explain,
)
from sqlguard.safety.executor.orphan_files import (
    OrphanFile,
    OrphanSafetyError,
    path_of,
    referenced_paths,
)
from sqlguard.safety.impact import compute_table_impact

_log = logging.getLogger("sqlguard.spark")

_BRANCH_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")
_NAME_PART = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]*$")
_SAFE_STRING = re.compile(r"^[A-Za-z0-9_.:/\-]+$")
_TARGET_TOKEN = "__sqlguard_branch_target__"
_RUNNABLE = (exp.Insert, exp.Update, exp.Delete, exp.Merge)
_READ_ONLY = (exp.Select, exp.SetOperation, exp.Subquery, exp.Describe)
_ERROR_LIMIT = 600

_WARNING_EXPIRE = (
    "Spark deletes the data and manifest files that no remaining snapshot needs; the expired "
    "history cannot be rebuilt."
)


class SparkSessionLike(Protocol):
    """The slice of `pyspark.sql.SparkSession` this adapter uses."""

    def sql(self, sqlQuery: str) -> Any: ...  # noqa: N803 - mirrors the pyspark signature


# -- SQL text building (pure, tested without Spark) ----------------------------------------------


def quote_part(part: str) -> str:
    """Backtick-quote one identifier part; only conservative names are accepted."""
    if not _NAME_PART.match(part):
        raise ExecutionError(f"unsupported identifier {part!r}")
    return f"`{part}`"


def table_identifier(spark_catalog: str, table: str) -> str:
    """`` `catalog`.`ns`.`name` `` for a qualified `ns.name` table."""
    namespace, _, name = table.rpartition(".")
    if not namespace:
        raise ExecutionError(f"table {table!r} must be qualified as namespace.name")
    return ".".join(quote_part(p) for p in (spark_catalog, *namespace.split("."), name))


def branch_identifier(spark_catalog: str, table: str, branch: str) -> str:
    """Identifier that makes a statement read and write one branch of `table`."""
    if not _BRANCH_NAME.match(branch):
        raise ExecutionError(f"invalid branch name {branch!r}")
    return f"{table_identifier(spark_catalog, table)}.{quote_part(f'branch_{branch}')}"


def _literal(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, datetime):
        moment = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return f"TIMESTAMP '{moment.astimezone(UTC).strftime('%Y-%m-%d %H:%M:%S.%f')}+00:00'"
    if isinstance(value, str):
        if not _SAFE_STRING.match(value):
            raise ExecutionError(f"unsupported characters in procedure argument {value!r}")
        return f"'{value}'"
    if isinstance(value, Sequence):
        return f"array({', '.join(_literal(v) for v in value)})"
    raise ExecutionError(f"cannot pass {type(value).__name__} to a procedure")


def procedure_call(spark_catalog: str, procedure: str, **arguments: Any) -> str:
    """`CALL catalog.system.procedure(name => value, ...)`; arguments that are None are omitted."""
    rendered = ", ".join(
        f"{name} => {_literal(value)}" for name, value in arguments.items() if value is not None
    )
    return f"CALL {quote_part(spark_catalog)}.system.{procedure}({rendered})"


def _key(namespace: str, name: str) -> str:
    return f"{namespace}.{name}".lower()


def spark_statement(
    ast: exp.Expression,
    *,
    spark_catalog: str,
    default_namespace: str,
    readable: Sequence[str],
    target: BranchHandle | None = None,
) -> str:
    """Render one parsed statement as Spark SQL.

    Tables in `readable` (qualified `ns.name`) are qualified to `spark_catalog`. When `target`
    is given, every reference to its table is rewritten to the branch identifier, and DML may
    only write that table. Anything else is refused rather than left to Spark's name resolution.
    """
    tree = ast.copy()
    if _TARGET_TOKEN in tree.sql():
        raise ExecutionError("statement uses a reserved identifier")
    known = {t.lower(): t for t in readable}
    ctes = {cte.alias.lower() for cte in tree.find_all(exp.CTE) if cte.alias}
    target_key = target.table.lower() if target else None
    if target is not None:
        known[target_key] = target.table

    if isinstance(tree, _RUNNABLE):
        written = tree.this.this if isinstance(tree.this, exp.Schema) else tree.this
        if target is None or not isinstance(written, exp.Table):
            raise UnsupportedAction("writes are only allowed against an audit branch")
        namespace = written.db or default_namespace
        if _key(namespace, written.name) != target_key or written.catalog:
            raise UnsupportedAction(
                f"statement writes {namespace}.{written.name}, not {target.table}"
            )

    for node in list(tree.find_all(exp.Table)):
        if not node.name:
            continue
        if not node.db and not node.catalog and node.name.lower() in ctes:
            continue
        if node.catalog:
            raise ExecutionError(f"catalog-qualified table {node.sql()} is not supported")
        namespace = node.db or default_namespace
        qualified = known.get(_key(namespace, node.name))
        if qualified is None:
            raise ExecutionError(
                f"table {namespace}.{node.name} is not among the tables cleared for this statement"
            )
        if qualified == (target.table if target else None):
            node.set("this", exp.to_identifier(_TARGET_TOKEN))
            node.set("db", None)
            node.set("catalog", None)
        else:
            ns, _, name = qualified.rpartition(".")
            node.set("this", exp.to_identifier(name, quoted=True))
            node.set("db", exp.to_identifier(ns, quoted=True))
            node.set("catalog", exp.to_identifier(spark_catalog, quoted=True))
    text = tree.sql(dialect="spark")
    if target is not None:
        text = text.replace(
            _TARGET_TOKEN, branch_identifier(spark_catalog, target.table, target.name)
        )
    return text


# -- Spark results to Arrow ----------------------------------------------------------------------

_ARROW_TYPES: dict[str, pa.DataType] = {
    "string": pa.string(),
    "tinyint": pa.int8(),
    "smallint": pa.int16(),
    "int": pa.int32(),
    "bigint": pa.int64(),
    "float": pa.float32(),
    "double": pa.float64(),
    "boolean": pa.bool_(),
    "date": pa.date32(),
    "timestamp": pa.timestamp("us", tz="UTC"),
    "timestamp_ntz": pa.timestamp("us"),
    "binary": pa.binary(),
}
_DECIMAL = re.compile(r"^decimal\((\d+),\s*(\d+)\)$")


def _arrow_type(spark_type: str) -> pa.DataType | None:
    if (match := _DECIMAL.match(spark_type)) is not None:
        return pa.decimal128(int(match.group(1)), int(match.group(2)))
    return _ARROW_TYPES.get(spark_type)


def _plain(value: Any) -> Any:
    if hasattr(value, "asDict"):
        return {k: _plain(v) for k, v in value.asDict().items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    return value


def _utc(value: Any) -> Any:
    # pyspark returns TIMESTAMP as a naive datetime in the process's zone; astimezone() reads
    # a naive value as local time, which is what it is.
    return value.astimezone(UTC) if isinstance(value, datetime) and value.tzinfo is None else value


def to_arrow(frame: Any) -> pa.Table:
    """Collect a Spark DataFrame into Arrow, keeping column types even when it has no rows."""
    fields = frame.schema.fields
    rows = frame.collect()
    arrays = []
    for index, field in enumerate(fields):
        arrow_type = _arrow_type(field.dataType.simpleString())
        values = [_plain(row[index]) for row in rows]
        if arrow_type is not None and getattr(arrow_type, "tz", None):
            values = [_utc(v) for v in values]
        try:
            arrays.append(pa.array(values, type=arrow_type))
        except (pa.ArrowException, ValueError, TypeError) as err:
            raise ExecutionError(f"cannot convert column {field.name}: {err}") from err
    return pa.table(arrays, names=[f.name for f in fields])


def _clip(text: str) -> str:
    text = text.split("JVM stacktrace:")[0].strip()  # the Java trace adds nothing for a reviewer
    return text if len(text) <= _ERROR_LIMIT else text[: _ERROR_LIMIT - 3] + "..."


# -- the adapter ---------------------------------------------------------------------------------


class SparkIcebergAdapter:
    def __init__(
        self,
        catalog: LakehouseCatalog,
        spark: SparkSessionLike,
        spark_catalog: str = "demo",
        *,
        prefix_listing: bool = True,
    ) -> None:
        """`prefix_listing` makes `remove_orphan_files` list files through the table's FileIO
        (S3FileIO, HadoopFileIO) instead of a Hadoop FileSystem, which object stores need unless
        the Hadoop connector for their scheme is installed."""
        quote_part(spark_catalog)
        self.catalog = catalog
        self.spark = spark
        self.spark_catalog = spark_catalog
        self.prefix_listing = prefix_listing
        self._meta = DuckDBIcebergAdapter(catalog)  # metadata reads and maintenance planning

    # -- plumbing --------------------------------------------------------------------------------

    def _frame(self, text: str) -> Any:
        _log.debug("spark: %s", text)
        try:
            return self.spark.sql(text)
        except Exception as err:  # noqa: BLE001 - pyspark raises many unrelated exception types
            raise ExecutionError(f"Spark rejected the statement: {_clip(str(err))}") from err

    def _run(self, text: str) -> list[Any]:
        frame = self._frame(text)
        try:
            return list(frame.collect())
        except Exception as err:  # noqa: BLE001 - see _frame
            raise ExecutionError(f"Spark failed the statement: {_clip(str(err))}") from err

    def _call(self, procedure: str, **arguments: Any) -> list[Any]:
        return self._run(procedure_call(self.spark_catalog, procedure, **arguments))

    def _table_ident(self, table: str) -> str:
        return table_identifier(self.spark_catalog, self.catalog.identifier(table))

    def _load(self, table: str) -> Table:
        try:
            return self.catalog.table(table)
        except NoSuchTableError as err:
            raise ExecutionError(f"table {table} does not exist") from err

    @staticmethod
    def _current_id(iceberg: Table) -> int | None:
        current = iceberg.current_snapshot()
        return current.snapshot_id if current else None

    @staticmethod
    def _branch_head(iceberg: Table, branch: BranchHandle) -> int:
        ref = iceberg.refs().get(branch.name)
        return ref.snapshot_id if ref else branch.base_snapshot_id

    # -- reads -----------------------------------------------------------------------------------

    def current_snapshot_id(self, table: str) -> int | None:
        return self._meta.current_snapshot_id(table)

    def snapshots(self, table: str) -> list[SnapshotInfo]:
        return self._meta.snapshots(table)

    def read(self, table: str, snapshot_id: int | None = None) -> pa.Table:
        return self._meta.read(table, snapshot_id)

    def query(self, sql: str, tables: Sequence[str], dialect: str = DEFAULT_DIALECT) -> pa.Table:
        statements = self._statements(sql, dialect, allowed=_READ_ONLY)
        readable = [self.catalog.identifier(t) for t in tables]
        for table in readable:
            self._load(table)
        texts = [
            _explain_wrap(
                explain,
                spark_statement(
                    ast,
                    spark_catalog=self.spark_catalog,
                    default_namespace=self.catalog.namespace,
                    readable=readable,
                ),
            )
            for explain, ast in statements
        ]
        for text in texts[:-1]:
            self._run(text)
        frame = self._frame(texts[-1])
        try:
            return to_arrow(frame)
        except ExecutionError:
            raise
        except Exception as err:  # noqa: BLE001 - see _frame
            raise ExecutionError(f"Spark failed the query: {_clip(str(err))}") from err

    # -- branches --------------------------------------------------------------------------------

    def create_branch(self, table: str, name: str | None = None) -> BranchHandle:
        branch = name or f"{AUDIT_BRANCH_PREFIX}{uuid.uuid4().hex[:10]}"
        if branch == MAIN or not _BRANCH_NAME.match(branch):
            raise ExecutionError(f"invalid branch name {branch!r}")
        iceberg = self._load(table)
        if branch in iceberg.refs():
            raise ExecutionError(f"branch {branch!r} already exists on {table}")
        base = iceberg.current_snapshot()
        if base is None:
            raise ExecutionError(
                f"{table} has no snapshot yet; a branch needs existing data to be cut from"
            )
        self._run(
            f"ALTER TABLE {self._table_ident(table)} CREATE BRANCH {quote_part(branch)} "
            f"AS OF VERSION {int(base.snapshot_id)}"
        )
        created = self._load(table).refs().get(branch)
        if created is None or created.snapshot_id != base.snapshot_id:
            raise ExecutionError(f"branch {branch} was not created on {table} as requested")
        return BranchHandle(self.catalog.identifier(table), branch, base.snapshot_id)

    def apply_sql(
        self,
        branch: BranchHandle,
        sql: str,
        dialect: str = DEFAULT_DIALECT,
        read_tables: Sequence[str] = (),
    ) -> None:
        statements = self._statements(sql, dialect, allowed=(*_RUNNABLE, *_READ_ONLY))
        readable = [self.catalog.identifier(t) for t in read_tables]
        texts = [
            _explain_wrap(
                explain,
                spark_statement(
                    ast,
                    spark_catalog=self.spark_catalog,
                    default_namespace=self.catalog.namespace,
                    readable=readable,
                    target=branch,
                ),
            )
            for explain, ast in statements
        ]
        for text in texts:
            self._run(text)

    def diff(self, branch: BranchHandle, key: Sequence[str] = ()) -> BranchDiff:
        iceberg = self._load(branch.table)
        before = self.catalog.read(branch.table, branch.base_snapshot_id)
        after = self.catalog.read(branch.table, self._branch_head(iceberg, branch))
        return BranchDiff(before, after, compute_table_impact(branch.table, before, after, key))

    def publish(self, branch: BranchHandle) -> PublishResult:
        iceberg = self._load(branch.table)
        main_id = self._current_id(iceberg)
        if main_id != branch.base_snapshot_id:
            raise BranchDiverged(
                f"main of {branch.table} moved from snapshot {branch.base_snapshot_id} to "
                f"{main_id} since the branch was created"
            )
        head = self._branch_head(iceberg, branch)
        if head == branch.base_snapshot_id:
            return PublishResult(branch.table, main_id, head, changed=False)
        if branch.base_snapshot_id not in self._meta._ancestry(iceberg, head):
            raise ExecutionError(f"branch {branch.name} does not descend from its base snapshot")
        try:
            self._call("fast_forward", table=branch.table, branch=MAIN, to=branch.name)
        except ExecutionError as err:
            now = self._current_id(self._load(branch.table))
            if now != main_id:
                raise BranchDiverged(
                    f"main of {branch.table} changed during publish: {err}"
                ) from err
            raise
        after = self._current_id(self._load(branch.table))
        if after != head:
            raise ExecutionError(
                f"fast_forward left {branch.table} at snapshot {after}, expected {head}"
            )
        return PublishResult(branch.table, main_id, head, changed=True)

    def drop_branch(self, branch: BranchHandle) -> None:
        if branch.name == MAIN:
            raise ExecutionError("refusing to drop main")
        self._run(
            f"ALTER TABLE {self._table_ident(branch.table)} "
            f"DROP BRANCH IF EXISTS {quote_part(branch.name)}"
        )

    def rollback(self, table: str, snapshot_id: int, expected_current: int | None = None) -> None:
        iceberg = self._load(table)
        current = self._current_id(iceberg)
        if expected_current is not None and current != expected_current:
            raise BranchDiverged(f"main of {table} is at {current}, expected {expected_current}")
        if iceberg.snapshot_by_id(snapshot_id) is None:
            raise ExecutionError(f"Cannot roll back to unknown snapshot id: {snapshot_id}")
        qualified = self.catalog.identifier(table)
        rows = self._call("rollback_to_snapshot", table=qualified, snapshot_id=int(snapshot_id))
        previous = int(rows[0][0]) if rows else current
        if expected_current is not None and previous != expected_current:
            # A writer committed between the check and the call; put its snapshot back.
            self._call("set_current_snapshot", table=qualified, snapshot_id=int(previous))
            raise BranchDiverged(
                f"main of {table} changed during rollback (was {previous}, expected "
                f"{expected_current}); the newer snapshot was restored"
            )

    # -- maintenance -----------------------------------------------------------------------------

    def plan_maintenance(self, action: MaintenanceAction) -> MaintenancePlan:
        if isinstance(action, RemoveOrphanFiles):
            return self._plan_orphans(action)
        plan = self._meta.plan_maintenance(action)
        if plan.kind == "expire_snapshots":
            return replace(plan, warnings=(*plan.warnings, _WARNING_EXPIRE))
        return plan

    def apply_maintenance(
        self, action: MaintenanceAction, plan: MaintenancePlan
    ) -> MaintenanceResult:
        iceberg = self._load(plan.table)
        before = self._current_id(iceberg)
        if plan.kind == "expire_snapshots":
            return self._apply_expire(iceberg, plan, before)
        if plan.kind == "remove_orphan_files":
            return self._apply_orphans(iceberg, action, plan, before)
        if before != plan.current_snapshot_id:
            raise ExecutionError(
                f"{plan.table} moved from snapshot {plan.current_snapshot_id} to {before} "
                "since the plan was made"
            )
        target = plan.target_snapshot_id
        if target is None:
            raise ExecutionError("plan has no target snapshot")
        if isinstance(action, FastForward):
            self._meta._require_publishable_ref(action.to)
            self._call("fast_forward", table=plan.table, branch=action.branch, to=action.to)
        elif isinstance(action, RollbackToSnapshot) and action.ref is None:
            self._call("rollback_to_snapshot", table=plan.table, snapshot_id=int(target))
        elif isinstance(action, RollbackToSnapshot):
            self._meta._require_publishable_ref(action.ref)
            self._call("set_current_snapshot", table=plan.table, snapshot_id=int(target))
        else:
            raise UnsupportedAction(f"unsupported maintenance action {type(action).__name__}")
        return MaintenanceResult(plan, before, self._current_id(self._load(plan.table)))

    def _apply_expire(
        self, iceberg: Table, plan: MaintenancePlan, before: int | None
    ) -> MaintenanceResult:
        existing = {s.snapshot_id for s in iceberg.snapshots()}
        protected = {ref.snapshot_id for ref in iceberg.refs().values()}
        ids = [s.snapshot_id for s in plan.expired if s.snapshot_id in existing - protected]
        if ids:
            self._call("expire_snapshots", table=plan.table, snapshot_ids=[int(i) for i in ids])
        return MaintenanceResult(plan, before, before, expired_snapshot_ids=tuple(ids))

    def _plan_orphans(self, action: RemoveOrphanFiles) -> MaintenancePlan:
        if action.older_than is None:
            raise ExecutionError("remove_orphan_files needs an explicit older_than")
        iceberg = self._load(action.table)
        self._meta._ensure_exclusive_location(iceberg)
        files = self._orphans(iceberg, action.table, action.older_than)
        return MaintenancePlan(
            kind="remove_orphan_files",
            table=self.catalog.identifier(action.table),
            irreversible=True,
            orphan_files=tuple(files),
            remaining_snapshots=len(iceberg.snapshots()),
            current_snapshot_id=self._current_id(iceberg),
        )

    def _orphans(self, iceberg: Table, table: str, older_than: datetime) -> list[OrphanFile]:
        """Spark's dry run, checked against the table metadata before anyone is asked to approve."""
        rows = self._call(
            "remove_orphan_files",
            table=self.catalog.identifier(table),
            older_than=older_than,
            dry_run=True,
            prefix_listing=self.prefix_listing,
        )
        locations = sorted({str(row[0]) for row in rows})
        try:
            self._check_orphans(iceberg, locations)
        except OrphanSafetyError as err:
            raise ExecutionError(str(err)) from err
        cutoff = older_than if older_than.tzinfo else older_than.replace(tzinfo=UTC)
        return [OrphanFile(loc, self._size(iceberg, loc), cutoff) for loc in locations]

    @staticmethod
    def _check_orphans(iceberg: Table, locations: Sequence[str]) -> None:
        """Refuse when Spark lists a file the table metadata still references (or one by name)."""
        referenced = referenced_paths(iceberg)
        live_names = {r.rsplit("/", 1)[-1] for r in referenced}
        root = path_of(iceberg.location()).rstrip("/") + "/"
        for location in locations:
            path = path_of(location)
            if path in referenced or path.rsplit("/", 1)[-1] in live_names:
                raise OrphanSafetyError(f"Spark lists a referenced file as orphan: {location}")
            if not path.startswith(root):
                raise OrphanSafetyError(
                    f"Spark lists a file outside the table location: {location}"
                )

    @staticmethod
    def _size(iceberg: Table, location: str) -> int:
        try:
            return len(iceberg.io.new_input(location))
        except Exception:  # noqa: BLE001 - size is informational; a missing file is not fatal here
            return 0

    def _apply_orphans(
        self,
        iceberg: Table,
        action: MaintenanceAction,
        plan: MaintenancePlan,
        before: int | None,
    ) -> MaintenanceResult:
        if not isinstance(action, RemoveOrphanFiles) or action.older_than is None:
            raise ExecutionError("remove_orphan_files needs an explicit older_than")
        self._meta._ensure_exclusive_location(iceberg)
        approved = {path_of(f.location) for f in plan.orphan_files}
        now = {f.location for f in self._orphans(iceberg, action.table, action.older_than)}
        unlisted = sorted(loc for loc in now if path_of(loc) not in approved)
        if unlisted:
            raise ExecutionError(
                f"{len(unlisted)} orphan file(s) were not in the approved plan, e.g. "
                f"{unlisted[0]}; nothing was deleted, plan again"
            )
        if not now:
            return MaintenanceResult(plan, before, before)
        rows = self._call(
            "remove_orphan_files",
            table=plan.table,
            older_than=action.older_than,
            dry_run=False,
            prefix_listing=self.prefix_listing,
        )
        removed = tuple(sorted({str(row[0]) for row in rows}))
        return MaintenanceResult(plan, before, before, deleted_files=removed)

    # -- helpers ---------------------------------------------------------------------------------

    @staticmethod
    def _statements(
        sql: str, dialect: str, allowed: tuple[type[exp.Expression], ...]
    ) -> list[tuple[Explain | None, exp.Expression]]:
        try:
            parsed = parse(sql, dialect)
        except SqlParseError as err:
            raise ExecutionError(str(err)) from err
        statements: list[tuple[Explain | None, exp.Expression]] = []
        for statement in parsed:
            explain = read_only_explain(statement.ast, dialect)
            if explain is not None:
                if explain.analyze:
                    raise UnsupportedAction("Spark has no EXPLAIN ANALYZE")
                statements.append((explain, explain.statement.ast))
                continue
            if not isinstance(statement.ast, allowed):
                raise UnsupportedAction(
                    f"{statement.kind} cannot be executed here; allowed: "
                    f"{', '.join(t.__name__ for t in allowed)}"
                )
            statements.append((None, statement.ast))
        return statements


def _explain_wrap(explain: Explain | None, text: str) -> str:
    return text if explain is None else explain.render(text)


__all__ = [
    "SparkIcebergAdapter",
    "SparkSessionLike",
    "branch_identifier",
    "procedure_call",
    "spark_statement",
    "table_identifier",
    "to_arrow",
]
