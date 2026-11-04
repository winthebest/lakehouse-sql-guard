"""Executor adapter: Trino as a read-only verifier of Iceberg tables, with two maintenance paths.

What Trino 484 can and cannot do for the Guard (verified on Trino 484 + Lakekeeper, see the
integration tests):

  * It reads main, any snapshot (`FOR VERSION AS OF <id>`) and any branch or tag
    (`FOR VERSION AS OF '<ref>'`), and exposes `"t$refs"` / `"t$snapshots"` / `table_changes`.
  * It cannot create a branch, write to one, or move a ref to a branch head with a guarded
    publish. Every DML it runs lands on main. This adapter therefore never rehearses a write:
    `create_branch`, `apply_sql` and `publish` raise `UnsupportedAction`, and with
    `branch_write=False` the Guard denies SQL writes ("no isolated rehearsal on trino").
  * `ALTER TABLE ... EXECUTE expire_snapshots(retention_threshold => ...)` takes only a duration.
    There is no dry run and no snapshot list, so it deletes what it deletes. It is used only when
    the set it would remove can be shown, beforehand, to equal the approved plan (see
    `_apply_expire`); otherwise the work goes to PyIceberg, which removes exactly the approved
    snapshot ids.
  * `remove_orphan_files(retention_threshold => ...)` has no dry run and no file list, so the
    Guard cannot show an approver what would go: `orphan_cleanup` is False and planning it is
    refused.
  * `rollback_to_snapshot` takes no "expected current snapshot". Worse, it accepts any snapshot
    id, including one that is not in main's history, so the adapter checks ancestry itself,
    checks that main is where the caller expects before the call, and reads the table's snapshot
    log afterwards to learn which snapshot the call actually replaced. If a writer slipped in
    between, the snapshot it replaced is put back (a catalog compare-and-swap through PyIceberg)
    and `BranchDiverged` is raised, the same restore-on-race as the Spark adapter.

Table metadata (snapshots, refs, ancestry, maintenance planning) comes from the PyIceberg catalog
the Guard already holds, through `DuckDBIcebergAdapter` (no DuckDB runs); Trino supplies row data,
query results and the engine's own view of refs and snapshots. Both must point at the same catalog.

`connection` is any DB-API style object with `cursor()`; `TrinoIcebergAdapter.connect` builds one
with the `trino` package, which is an optional dependency and never imported here otherwise.
Results are converted to Arrow with an explicit type map (nested types are refused rather than
guessed) and a hard row cap: a result larger than `max_rows` is an error, never a silent cut.
"""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import pyarrow as pa
from pyiceberg.exceptions import CommitFailedException
from pyiceberg.table import Table
from sqlglot import exp

from sqlguard.core.catalog import LakehouseCatalog, SnapshotInfo
from sqlguard.core.parsing import DEFAULT_DIALECT, Explain, SqlParseError, parse
from sqlguard.safety.actions import (
    ExpireSnapshots,
    MaintenanceAction,
    RemoveOrphanFiles,
    RollbackToSnapshot,
)
from sqlguard.safety.classifier import is_cte_reference
from sqlguard.safety.executor.base import (
    BranchDiff,
    BranchDiverged,
    BranchHandle,
    Capabilities,
    ExecutionError,
    MaintenancePlan,
    MaintenanceResult,
    PublishResult,
    UnsupportedAction,
)
from sqlguard.safety.executor.duckdb_iceberg import (
    DuckDBIcebergAdapter,
    read_only_explain,
)
from sqlguard.safety.impact import compute_table_impact

_log = logging.getLogger("sqlguard.trino")

DEFAULT_MAX_ROWS = 1_000_000
# A snapshot this close to the expiry cutoff makes "what Trino would expire" depend on the clock
# difference between this process and Trino, so such an expiry is left to PyIceberg.
DEFAULT_CLOCK_MARGIN = timedelta(minutes=5)

_NAME_PART = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]*$")
_REF_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")
_READ_ONLY = (exp.Select, exp.SetOperation, exp.Subquery, exp.Describe)
_ERROR_LIMIT = 600

_NO_BRANCH_WRITES = (
    "Trino cannot create branches or write to them; use a write engine (PyIceberg or Spark)"
)
_WARNING_EXPIRE = (
    "Trino deletes the data and manifest files that no remaining snapshot needs and may run this "
    "expiry; it has no dry run, so the removed set is checked before and after, not previewed, "
    "and the expired history cannot be rebuilt."
)


class MaintenanceNeedsAttention(ExecutionError):
    """Trino ran the maintenance, then the table was found in a state nobody approved.

    Unlike a plain `ExecutionError` ("the engine rejected the statement; nothing happened"), the
    change is live and may not be undoable: `executed` is True so a caller can record it as such
    and ask a person to look.
    """

    executed = True


class RollbackNeedsAttention(MaintenanceNeedsAttention, BranchDiverged):
    """A Trino rollback ran and main is not where the caller can accept (a commit was displaced
    and could not be put back, or main moved on after the rollback)."""


class TrinoCursorLike(Protocol):
    """The slice of a `trino.dbapi.Cursor` this adapter uses."""

    @property
    def description(self) -> Sequence[Sequence[Any]] | None: ...

    def execute(self, operation: str, params: Any = None) -> Any: ...

    def fetchmany(self, size: int = ...) -> list[Any]: ...

    def fetchall(self) -> list[Any]: ...

    def close(self) -> None: ...


class TrinoConnectionLike(Protocol):
    def cursor(self) -> TrinoCursorLike: ...


# -- SQL text building (pure) ---------------------------------------------------------------------


def _quote(part: str) -> str:
    if not _NAME_PART.fullmatch(part):
        raise ExecutionError(f"unsupported identifier {part!r}")
    return f'"{part}"'


def table_identifier(trino_catalog: str, table: str, *, suffix: str = "") -> str:
    """`"catalog"."ns"."name"` for a qualified `ns.name` table; `suffix` is a metadata table
    such as `$refs` (only the fixed Iceberg ones are accepted)."""
    namespace, _, name = table.rpartition(".")
    if not namespace:
        raise ExecutionError(f"table {table!r} must be qualified as namespace.name")
    if "." in namespace:
        raise ExecutionError(f"nested namespace {namespace!r} is not supported on Trino")
    if suffix and suffix not in ("$refs", "$snapshots"):
        raise ExecutionError(f"unsupported metadata table {suffix!r}")
    return f'{_quote(trino_catalog)}.{_quote(namespace)}."{_checked(name)}{suffix}"'


def _checked(part: str) -> str:
    """`part` unchanged, after refusing anything that is not a conservative identifier."""
    _quote(part)
    return part


def _version_clause(version: int | str) -> str:
    if isinstance(version, bool):
        raise ExecutionError("invalid table version")
    if isinstance(version, int):
        return f"FOR VERSION AS OF {version}"
    if not _REF_NAME.fullmatch(version):
        raise ExecutionError(f"invalid ref name {version!r}")
    return f"FOR VERSION AS OF '{version}'"


def read_statement(
    trino_catalog: str, table: str, *, snapshot_id: int | None = None, ref: str | None = None
) -> str:
    """`SELECT *` of a table at main, a snapshot id, or a named branch/tag."""
    text = f"SELECT * FROM {table_identifier(trino_catalog, table)}"
    if snapshot_id is not None and ref is not None:
        raise ExecutionError("give a snapshot id or a ref, not both")
    if snapshot_id is not None:
        text += f" {_version_clause(int(snapshot_id))}"
    elif ref is not None:
        text += f" {_version_clause(ref)}"
    return text


def trino_statement(
    ast: exp.Expression, *, trino_catalog: str, default_namespace: str, readable: Sequence[str]
) -> str:
    """Render one read-only statement as Trino SQL with every table qualified to the catalog.

    A table that is not in `readable` (qualified `ns.name`), that names a catalog, or that is a
    table function is refused rather than left to Trino's name resolution, so a query cannot
    reach another catalog or an uncleared table. (`UNNEST` and `VALUES` are not tables to sqlglot
    and pass.)
    """
    tree = ast.copy()
    known = {t.lower(): t for t in readable}
    for node in list(tree.find_all(exp.Table)):
        if not isinstance(node.this, exp.Identifier):
            # A table function (TABLE(...), system.query, table_changes, read_parquet, ...) is not
            # a table of the cleared list and can reach other catalogs or tables: fail closed.
            raise ExecutionError(f"table function {node.sql()} is not supported")
        if is_cte_reference(node):
            continue
        if node.catalog:
            raise ExecutionError(f"catalog-qualified table {node.sql()} is not supported")
        namespace = node.db or default_namespace
        qualified = known.get(f"{namespace}.{node.name}".lower())
        if qualified is None:
            raise ExecutionError(
                f"table {namespace}.{node.name} is not among the tables cleared for this statement"
            )
        ns, _, name = qualified.rpartition(".")
        node.set("this", exp.to_identifier(name, quoted=True))
        node.set("db", exp.to_identifier(ns, quoted=True))
        node.set("catalog", exp.to_identifier(trino_catalog, quoted=True))
    return tree.sql(dialect="trino")


# -- Trino results to Arrow -----------------------------------------------------------------------

_ARROW_TYPES: dict[str, pa.DataType] = {
    "boolean": pa.bool_(),
    "tinyint": pa.int8(),
    "smallint": pa.int16(),
    "integer": pa.int32(),
    "bigint": pa.int64(),
    "real": pa.float32(),
    "double": pa.float64(),
    "varchar": pa.string(),
    "char": pa.string(),
    "uuid": pa.string(),
    "varbinary": pa.binary(),
    "date": pa.date32(),
}
_TYPE = re.compile(r"^(?P<base>[a-z]+)\s*(?:\((?P<args>[^)]*)\))?(?P<tz>\s+with\s+time\s+zone)?$")


def arrow_type(trino_type: str) -> pa.DataType | None:
    """Arrow type for a Trino type name, or None when the type is not supported."""
    match = _TYPE.fullmatch(trino_type.strip().lower())
    if match is None:
        return None
    base, args, with_tz = match["base"], match["args"], match["tz"] is not None
    if base in ("timestamp", "time"):
        # Arrow gets microseconds, so finer precision would silently lose changes: refuse it.
        if args is not None and (not args.strip().isdigit() or int(args) > 6):
            return None
        if base == "timestamp":
            return pa.timestamp("us", tz="UTC" if with_tz else None)
        return None if with_tz else pa.time64("us")
    if with_tz:
        return None
    if base == "decimal":
        try:
            precision, scale = (int(p) for p in (args or "").split(","))
        except ValueError:
            return None
        return pa.decimal128(precision, scale) if precision <= 38 else None
    return _ARROW_TYPES.get(base)


def _convert(trino_type: str, value: Any) -> Any:
    if value is None:
        return None
    if trino_type.startswith("uuid"):
        return str(value)
    return value


def rows_to_arrow(description: Sequence[Sequence[Any]], rows: Sequence[Sequence[Any]]) -> pa.Table:
    """Arrow table of a Trino result, typed from the cursor description even with no rows."""
    arrays: list[pa.Array] = []
    names: list[str] = []
    for index, column in enumerate(description):
        name, trino_type = str(column[0]), str(column[1])
        target = arrow_type(trino_type)
        if target is None:
            raise ExecutionError(
                f"cannot convert column {name!r} of Trino type {trino_type!r} to Arrow"
            )
        values = [_convert(trino_type, row[index]) for row in rows]
        try:
            arrays.append(pa.array(values, type=target))
        except (pa.ArrowException, ValueError, TypeError) as err:
            raise ExecutionError(f"cannot convert column {name!r}: {err}") from err
        names.append(name)
    return pa.table(arrays, names=names)


def _clip(text: str) -> str:
    text = text.strip()
    return text if len(text) <= _ERROR_LIMIT else text[: _ERROR_LIMIT - 3] + "..."


# -- the adapter ----------------------------------------------------------------------------------


class TrinoIcebergAdapter:
    engine = "trino"
    capabilities = Capabilities(rollback=True, expire=True, changelog=True)

    def __init__(
        self,
        catalog: LakehouseCatalog,
        connection: TrinoConnectionLike,
        trino_catalog: str = "lk",
        *,
        max_rows: int = DEFAULT_MAX_ROWS,
        clock_margin: timedelta = DEFAULT_CLOCK_MARGIN,
    ) -> None:
        """`trino_catalog` is the Trino catalog serving the same Iceberg catalog as `catalog`."""
        _quote(trino_catalog)
        if max_rows < 1:
            raise ValueError("max_rows must be positive")
        self.catalog = catalog
        self.connection = connection
        self.trino_catalog = trino_catalog
        self.max_rows = max_rows
        self.clock_margin = clock_margin
        self._meta = DuckDBIcebergAdapter(catalog)  # metadata reads and maintenance planning

    @classmethod
    def connect(
        cls,
        catalog: LakehouseCatalog,
        *,
        host: str,
        port: int = 8080,
        user: str = "sqlguard",
        trino_catalog: str = "lk",
        **options: Any,
    ) -> TrinoIcebergAdapter:
        """Adapter over a `trino.dbapi` connection (needs the optional `trino` package)."""
        try:
            from trino.dbapi import connect
        except ImportError as err:
            raise ExecutionError(
                "the Trino adapter needs the `trino` package (pip install trino)"
            ) from err
        options.setdefault("timezone", "UTC")
        connection = connect(host=host, port=port, user=user, catalog=trino_catalog, **options)
        return cls(catalog, connection, trino_catalog)

    # -- plumbing --------------------------------------------------------------------------------

    def _fetch(self, text: str, *, limit: int | None = None) -> pa.Table:
        """Run `text` and return its rows as Arrow; more than `limit` rows is an error."""
        cap = self.max_rows if limit is None else limit
        _log.debug("trino: %s", text)
        cursor = self.connection.cursor()
        try:
            cursor.execute(text)
            rows = cursor.fetchmany(cap + 1)
            description = cursor.description
        except Exception as err:  # noqa: BLE001 - the trino client raises many unrelated types
            raise ExecutionError(f"Trino rejected the statement: {_clip(str(err))}") from err
        finally:
            _close(cursor)
        if len(rows) > cap:
            raise ExecutionError(f"the result has more than {cap} rows; narrow the query")
        if description is None:
            raise ExecutionError("Trino returned no result set for a read")
        return rows_to_arrow(description, rows)

    def _run(self, text: str) -> None:
        """Run a statement that returns nothing we need (ALTER TABLE ... EXECUTE)."""
        _log.debug("trino: %s", text)
        cursor = self.connection.cursor()
        try:
            cursor.execute(text)
            cursor.fetchall()  # the statement only completes when its result is drained
        except Exception as err:  # noqa: BLE001 - see _fetch
            raise ExecutionError(f"Trino failed the statement: {_clip(str(err))}") from err
        finally:
            _close(cursor)

    def _ident(self, table: str, suffix: str = "") -> str:
        return table_identifier(self.trino_catalog, self.catalog.identifier(table), suffix=suffix)

    # -- reads -----------------------------------------------------------------------------------

    def current_snapshot_id(self, table: str) -> int | None:
        return self._meta.current_snapshot_id(table)

    def snapshots(self, table: str) -> list[SnapshotInfo]:
        return self._meta.snapshots(table)

    def read(self, table: str, snapshot_id: int | None = None) -> pa.Table:
        self._meta._load(table)
        return self._fetch(
            read_statement(
                self.trino_catalog, self.catalog.identifier(table), snapshot_id=snapshot_id
            )
        )

    def read_ref(self, table: str, ref: str) -> pa.Table:
        """The table as a branch or tag sees it (`FOR VERSION AS OF '<ref>'`)."""
        self._meta._load(table)
        return self._fetch(
            read_statement(self.trino_catalog, self.catalog.identifier(table), ref=ref)
        )

    def refs(self, table: str) -> dict[str, int]:
        """Branch and tag heads as Trino sees them (`"t$refs"`)."""
        rows = self._fetch(f"SELECT name, snapshot_id FROM {self._ident(table, '$refs')}")
        names, ids = rows.column("name").to_pylist(), rows.column("snapshot_id").to_pylist()
        return dict(zip(names, ids, strict=True))

    def engine_snapshots(self, table: str) -> list[tuple[int, int | None]]:
        """`(snapshot_id, parent_id)` pairs as Trino sees them (`"t$snapshots"`), oldest first."""
        rows = self._fetch(
            f"SELECT snapshot_id, parent_id FROM {self._ident(table, '$snapshots')} "
            "ORDER BY committed_at"
        )
        ids, parents = rows.column("snapshot_id").to_pylist(), rows.column("parent_id").to_pylist()
        return list(zip(ids, parents, strict=True))

    def changes(self, table: str, start_snapshot_id: int, end_snapshot_id: int) -> pa.Table:
        """Row changes between two snapshots (`table_changes`; no delete-file support in Trino)."""
        namespace, _, name = self.catalog.identifier(table).rpartition(".")
        return self._fetch(
            f"SELECT * FROM TABLE({_quote(self.trino_catalog)}.system.table_changes("
            f"schema_name => '{_checked(namespace)}', table_name => '{_checked(name)}', "
            f"start_snapshot_id => {int(start_snapshot_id)}, "
            f"end_snapshot_id => {int(end_snapshot_id)}))"
        )

    def query(self, sql: str, tables: Sequence[str], dialect: str = DEFAULT_DIALECT) -> pa.Table:
        statements = self._statements(sql, dialect)
        readable = [self.catalog.identifier(t) for t in tables]
        for table in readable:
            self._meta._load(table)
        texts = [
            _explain_wrap(
                explain,
                trino_statement(
                    ast,
                    trino_catalog=self.trino_catalog,
                    default_namespace=self.catalog.namespace,
                    readable=readable,
                ),
            )
            for explain, ast in statements
        ]
        for text in texts[:-1]:
            self._fetch(text)
        return self._fetch(texts[-1])

    # -- branches: Trino cannot write them -------------------------------------------------------

    def create_branch(self, table: str, name: str | None = None) -> BranchHandle:
        raise UnsupportedAction(_NO_BRANCH_WRITES)

    def apply_sql(
        self,
        branch: BranchHandle,
        sql: str,
        dialect: str = DEFAULT_DIALECT,
        read_tables: Sequence[str] = (),
    ) -> None:
        raise UnsupportedAction(_NO_BRANCH_WRITES)

    def publish(self, branch: BranchHandle, expected_head: int) -> PublishResult:
        raise UnsupportedAction("Trino cannot publish a branch; use a write engine")

    def drop_branch(self, branch: BranchHandle) -> None:
        raise UnsupportedAction(_NO_BRANCH_WRITES)

    def diff(self, branch: BranchHandle, key: Sequence[str] = ()) -> BranchDiff:
        """Measure a branch written by another engine, reading both sides through Trino.

        The head comes from Trino's own `$refs`, and both sides are read by snapshot id, so the
        measured data and the recorded head cannot disagree.
        """
        head = self.refs(branch.table).get(branch.name)
        if head is None:
            raise ExecutionError(f"Trino does not see branch {branch.name!r} on {branch.table}")
        before = self.read(branch.table, branch.base_snapshot_id)
        after = self.read(branch.table, head)
        return BranchDiff(
            before,
            after,
            compute_table_impact(branch.table, before, after, key),
            replace(branch, head_snapshot_id=head),
        )

    # -- rollback --------------------------------------------------------------------------------

    def rollback(self, table: str, snapshot_id: int, expected_current: int | None = None) -> None:
        iceberg = self._meta._load(table)
        current = self._meta._current_id(iceberg)
        if expected_current is not None and current != expected_current:
            raise BranchDiverged(f"main of {table} is at {current}, expected {expected_current}")
        if iceberg.snapshot_by_id(snapshot_id) is None:
            raise ExecutionError(f"Cannot roll back to unknown snapshot id: {snapshot_id}")
        if snapshot_id not in self._meta._ancestry(iceberg, current):
            # Trino would happily point main at any snapshot, a branch head included.
            raise ExecutionError(
                f"Cannot roll back to snapshot, not an ancestor of the current state: {snapshot_id}"
            )
        if snapshot_id == current:
            return
        # Without an explicit expectation, main as observed just now is the expectation, so a
        # writer that commits before Trino loads the table is never dropped silently.
        expected = current if expected_current is None else expected_current
        refs = self._ref_state(iceberg)
        self._run(
            f"ALTER TABLE {self._ident(table)} EXECUTE rollback_to_snapshot("
            f"snapshot_id => {int(snapshot_id)})"
        )
        self._verify_rollback(table, snapshot_id, current, expected)
        self._check_refs(table, refs, ignore_main=True)

    def _replaced_by_rollback(self, after: Table, target: int) -> int | None:
        """The snapshot a logged rollback to `target` replaced, or None when the log does not show
        one. The entry before the last one for `target` counts only if it descends from `target`:
        a rollback replaces a newer snapshot, while the entry that originally made `target` main
        follows an older one. Moving main to an older snapshot is never a restore."""
        log = after.history()
        for index in range(len(log) - 1, 0, -1):
            if log[index].snapshot_id == target:
                previous = log[index - 1].snapshot_id
                lineage = self._meta._ancestry(after, previous)
                return previous if previous != target and target in lineage else None
        return None

    def _verify_rollback(self, table: str, target: int, before: int | None, expected: int) -> None:
        """Check, from the table's snapshot log, which snapshot the rollback replaced.

        If the catalog's log does not show the rollback, what it replaced is unknown: nothing is
        restored (that could move main to an older snapshot) and the outcome is reported.
        """
        after = self._meta._load(table)
        now = self._meta._current_id(after)
        replaced = self._replaced_by_rollback(after, target)
        if now != target:
            lost = ""
            if replaced is not None and replaced != expected:
                lost = (
                    f" The rollback replaced snapshot {replaced} (expected {expected}); that "
                    "commit is no longer on main, though the snapshot still exists and can be "
                    "restored."
                )
            raise RollbackNeedsAttention(
                f"main of {table} is at {now} after rolling back to {target}; it changed during "
                f"the rollback and was left as is.{lost}"
            )
        if replaced is None:
            raise RollbackNeedsAttention(
                f"main of {table} is at {target} after the rollback, but the snapshot log does not "
                "show which snapshot it replaced, so a commit displaced by it cannot be ruled out; "
                "nothing was restored"
            )
        if replaced == expected:
            return
        # A writer committed between our check and Trino's load of the table; put its snapshot
        # back. `after` was just seen at the target, so the commit asserts main is still there.
        try:
            after.manage_snapshots().set_current_snapshot(snapshot_id=replaced).commit()
        except (CommitFailedException, ValueError) as err:
            raise RollbackNeedsAttention(
                f"main of {table} changed during rollback (was {replaced}, expected "
                f"{expected}) and could not be restored: {err}"
            ) from err
        raise BranchDiverged(
            f"main of {table} changed during rollback (was {replaced}, expected "
            f"{expected}); the newer snapshot was restored"
        )

    # -- maintenance -----------------------------------------------------------------------------

    @staticmethod
    def _ref_state(iceberg: Table) -> dict[str, tuple[str, int]]:
        return {
            name: (
                str(getattr(ref.snapshot_ref_type, "value", ref.snapshot_ref_type)),
                ref.snapshot_id,
            )
            for name, ref in iceberg.refs().items()
        }

    def _check_refs(
        self, table: str, before: dict[str, tuple[str, int]], *, ignore_main: bool
    ) -> None:
        """Report a branch or tag that existed before a Trino run and is now gone, changed kind,
        or moved off its own history. A ref that merely advanced (an ordinary commit to it while
        Trino worked) is fine."""
        iceberg = self._meta._load(table)
        after = self._ref_state(iceberg)
        changed = []
        for name, (kind, head) in before.items():
            if ignore_main and name == "main":
                continue
            state = after.get(name)
            if state is None or state[0] != kind:
                gone = True
            else:
                gone = state[1] != head and head not in self._meta._ancestry(iceberg, state[1])
            if gone:
                changed.append(name)
        if changed:
            raise MaintenanceNeedsAttention(
                f"Trino ran, then ref(s) {sorted(changed)} of {table} turned out removed or moved "
                "off their history, which no approved plan covers; check the table's metadata "
                "history to restore them"
            )

    def plan_maintenance(self, action: MaintenanceAction) -> MaintenancePlan:
        if isinstance(action, ExpireSnapshots):
            plan = self._meta.plan_maintenance(action)
            if self._trino_may_expire(action):
                return replace(plan, warnings=(*plan.warnings, _WARNING_EXPIRE))
            return plan
        if isinstance(action, RollbackToSnapshot):
            if action.ref is not None:
                raise UnsupportedAction("Trino rolls back to a snapshot of main, not to a ref")
            return self._meta.plan_maintenance(action)
        if isinstance(action, RemoveOrphanFiles):
            raise UnsupportedAction(
                "Trino lists no orphan files before deleting (no dry run); orphan cleanup is "
                "not available on this engine"
            )
        raise UnsupportedAction(f"{type(action).__name__} is not executed by the Trino adapter")

    def apply_maintenance(
        self, action: MaintenanceAction, plan: MaintenancePlan
    ) -> MaintenanceResult:
        iceberg = self._meta._load(plan.table)
        before = self._meta._current_id(iceberg)
        if isinstance(action, ExpireSnapshots) and plan.kind == "expire_snapshots":
            return self._apply_expire(action, plan, before)
        if (
            isinstance(action, RollbackToSnapshot)
            and plan.kind == "rollback"
            and action.ref is None
        ):
            if before != plan.current_snapshot_id:
                raise ExecutionError(
                    f"{plan.table} moved from snapshot {plan.current_snapshot_id} to {before} "
                    "since the plan was made"
                )
            if plan.target_snapshot_id is None:
                raise ExecutionError("plan has no target snapshot")
            self.rollback(plan.table, plan.target_snapshot_id, expected_current=before)
            return MaintenanceResult(plan, before, self._meta.current_snapshot_id(plan.table))
        raise UnsupportedAction(f"unsupported maintenance action {type(action).__name__}")

    def _plan_ids(self, action: ExpireSnapshots, older_than: datetime) -> set[int]:
        replanned = self._meta.plan_maintenance(replace(action, older_than=older_than))
        return {s.snapshot_id for s in replanned.expired}

    def _trino_may_expire(self, action: ExpireSnapshots) -> bool:
        """True when the shape of the table and the action leave Trino's expiry equal to the plan.

        Iceberg's expiry (which Trino runs) removes more than snapshots older than the cutoff: it
        also drops every branch or tag past its max ref age (its own, or the table default
        `history.expire.max-ref-age-ms`) together with the snapshots only they kept, applies
        per-ref snapshot-age and minimum-count rules, and honours the other `history.expire.*`
        properties. The planner treats every ref head as protected and cannot see any of that, so
        Trino is only trusted on a table whose single ref is a plain `main` with no retention
        settings anywhere. Also required: an age cutoff alone (no explicit ids, `retain_last`
        at most 1).
        """
        if action.older_than is None or action.snapshot_ids or (action.retain_last or 1) > 1:
            return False
        try:
            iceberg = self._meta._load(action.table)
        except ExecutionError:
            return False
        refs = iceberg.refs()
        if set(refs) - {"main"}:
            return False
        main = refs.get("main")
        if main is not None and (
            main.max_snapshot_age_ms is not None
            or main.min_snapshots_to_keep is not None
            or main.max_ref_age_ms is not None
        ):
            return False
        return not any(key.startswith("history.expire.") for key in iceberg.properties)

    def _trino_threshold(self, action: ExpireSnapshots, plan: MaintenancePlan) -> int | None:
        """Seconds for `retention_threshold` when Trino may run this expiry now, else None.

        On top of `_trino_may_expire`: the cutoff must lie in the past, and no snapshot may sit
        within `clock_margin` of it, or the clock difference between here and Trino would change
        the outcome.
        """
        if not self._trino_may_expire(action):
            return None
        approved = {s.snapshot_id for s in plan.expired}
        cutoff = action.older_than
        assert cutoff is not None  # _trino_may_expire
        cutoff = cutoff if cutoff.tzinfo else cutoff.replace(tzinfo=UTC)
        if cutoff >= datetime.now(UTC):
            return None
        if not (
            self._plan_ids(action, cutoff - self.clock_margin)
            == self._plan_ids(action, cutoff + self.clock_margin)
            == approved
        ):
            return None
        return max(1, math.ceil((datetime.now(UTC) - cutoff).total_seconds()))

    def _apply_expire(
        self, action: ExpireSnapshots, plan: MaintenancePlan, before: int | None
    ) -> MaintenanceResult:
        """Let Trino expire only when it will remove exactly the approved snapshots.

        Trino runs only if the approver was shown the warning that it would (decided when the
        plan was made) and the table is still in a shape where Trino's expiry equals the plan
        (`_trino_may_expire`, `_trino_threshold`, checked again now). Otherwise, or if Trino
        refuses its own retention floor, PyIceberg expires the approved ids (never more).
        Trino has no dry run, so afterwards the removed snapshots and every ref are compared with
        the plan and a difference is reported (it cannot be undone).
        """
        approved = {s.snapshot_id for s in plan.expired}
        current_plan = self._meta.plan_maintenance(action)
        threshold = (
            self._trino_threshold(action, current_plan)
            if _WARNING_EXPIRE in plan.warnings
            and {s.snapshot_id for s in current_plan.expired} == approved
            else None
        )
        if threshold is None:
            return self._meta.apply_maintenance(action, plan)
        loaded = self._meta._load(plan.table)
        existing = {s.snapshot_id for s in loaded.snapshots()}
        refs = self._ref_state(loaded)
        try:
            self._run(
                f"ALTER TABLE {self._ident(plan.table)} EXECUTE "
                f"expire_snapshots(retention_threshold => '{threshold}s')"
            )
        except ExecutionError as err:
            if "minimum retention" not in str(err):
                raise
            _log.info("Trino refused the retention (%s); expiring through PyIceberg", err)
            return self._meta.apply_maintenance(action, plan)
        iceberg = self._meta._load(plan.table)
        removed = existing - {s.snapshot_id for s in iceberg.snapshots()}
        extra = sorted(removed - approved)
        if extra:
            raise MaintenanceNeedsAttention(
                f"Trino expired {len(extra)} snapshot(s) outside the approved plan, e.g. "
                f"{extra[0]}; the expired history cannot be restored"
            )
        self._check_refs(plan.table, refs, ignore_main=False)
        return MaintenanceResult(
            plan,
            before,
            self._meta._current_id(iceberg),
            expired_snapshot_ids=tuple(sorted(removed)),
        )

    # -- helpers ---------------------------------------------------------------------------------

    @staticmethod
    def _statements(sql: str, dialect: str) -> list[tuple[Explain | None, exp.Expression]]:
        try:
            parsed = parse(sql, dialect)
        except SqlParseError as err:
            raise ExecutionError(str(err)) from err
        statements: list[tuple[Explain | None, exp.Expression]] = []
        for statement in parsed:
            explain = read_only_explain(statement.ast, dialect)
            if explain is not None:
                if explain.analyze:
                    raise UnsupportedAction("EXPLAIN ANALYZE is not supported on Trino")
                statements.append((explain, explain.statement.ast))
                continue
            if not isinstance(statement.ast, _READ_ONLY):
                raise UnsupportedAction(
                    f"{statement.kind} is not read-only; Trino only answers queries here "
                    "(no isolated rehearsal of writes)"
                )
            statements.append((None, statement.ast))
        return statements


def _close(cursor: TrinoCursorLike) -> None:
    try:
        cursor.close()
    except Exception:  # noqa: BLE001 - closing a finished cursor must not mask the real result
        _log.debug("could not close a Trino cursor", exc_info=True)


def _explain_wrap(explain: Explain | None, text: str) -> str:
    return text if explain is None else explain.render(text)


__all__ = [
    "TrinoConnectionLike",
    "MaintenanceNeedsAttention",
    "RollbackNeedsAttention",
    "TrinoCursorLike",
    "TrinoIcebergAdapter",
    "arrow_type",
    "read_statement",
    "rows_to_arrow",
    "table_identifier",
    "trino_statement",
]
