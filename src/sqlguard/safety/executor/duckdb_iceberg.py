"""Executor adapter v1: PyIceberg for storage and refs, DuckDB for running DML.

How a write is executed
  1. `create_branch` cuts an Iceberg branch from main's current snapshot.
  2. `apply_sql` reads the branch head into an in-memory DuckDB, runs the statements there, and
     writes the whole resulting table back to the branch (`Table.overwrite(..., branch=...)`).
     This is copy-on-write of the entire table, which is fine at benchmark scale and not meant
     for large tables (a Spark adapter would handle those). DuckDB runs with external access
     disabled and only the registered Arrow tables visible.
  3. `diff` records the branch head it measured. `publish(branch, expected_head)` moves main to
     exactly that snapshot with `set_current_snapshot` (PyIceberg has no fast-forward, and a
     fast-forward would follow the branch ref, taking anything written to it after approval). It
     first checks that main still sits on the snapshot the branch was cut from, and the commit
     itself carries PyIceberg's `AssertRefSnapshotId` requirement, so a concurrent writer makes
     the publish fail instead of being overwritten. Afterwards main must sit on `expected_head`.

Behaviours verified against pyiceberg 0.12 and locked by tests (a version bump that changes them
turns those tests red): branch overwrite leaves main untouched; `rollback_to_snapshot` only moves
the pointer; `expire_snapshots` removes metadata only, so Parquet files stay on disk until
`remove_orphan_files` runs; rolling back to an expired snapshot raises
"Cannot roll back to unknown snapshot id".

Limits: tables must already have a snapshot (a branch needs something to be cut from), and one
action may write one table.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime

import duckdb
import pyarrow as pa
from pyiceberg.exceptions import CommitFailedException, NoSuchTableError
from pyiceberg.table import Table
from sqlglot import exp

from sqlguard.core.catalog import LakehouseCatalog, SnapshotInfo
from sqlguard.core.parsing import DEFAULT_DIALECT, SqlParseError, explained, parse
from sqlguard.safety.actions import (
    DropTable,
    ExpireSnapshots,
    FastForward,
    MaintenanceAction,
    RemoveOrphanFiles,
    RollbackToSnapshot,
)
from sqlguard.safety.executor.base import (
    BranchDiff,
    BranchDiverged,
    BranchHandle,
    Capabilities,
    ExecutionError,
    MaintenancePlan,
    MaintenanceResult,
    PlannedSnapshot,
    PublishMismatch,
    PublishResult,
    UnsupportedAction,
)
from sqlguard.safety.executor.orphan_files import (
    OrphanSafetyError,
    delete_orphans,
    find_orphans,
    locations_overlap,
)
from sqlguard.safety.impact import compute_table_impact, quote_ident

MAIN = "main"
AUDIT_BRANCH_PREFIX = "sqlguard-"  # branches the guard creates; never valid publish targets
_BRANCH_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")
_RUNNABLE = (exp.Insert, exp.Update, exp.Delete, exp.Merge)
_READ_ONLY = (exp.Select, exp.SetOperation, exp.Subquery, exp.Describe)


def _millis(moment: datetime) -> int:
    aware = moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)
    return int(aware.timestamp() * 1000)


def _connect(tables: dict[str, pa.Table], default_namespace: str) -> duckdb.DuckDBPyConnection:
    """In-memory DuckDB with each `ns.name` Arrow table materialized and no external access."""
    conn = duckdb.connect(config={"enable_external_access": False})
    conn.execute("SET TimeZone = 'UTC'")
    conn.execute(f"CREATE SCHEMA IF NOT EXISTS {quote_ident(default_namespace)}")
    for i, (qualified, data) in enumerate(tables.items()):
        namespace, _, name = qualified.rpartition(".")
        conn.execute(f"CREATE SCHEMA IF NOT EXISTS {quote_ident(namespace)}")
        view = f"__arrow_{i}"
        conn.register(view, data)
        conn.execute(
            f"CREATE TABLE {quote_ident(namespace)}.{quote_ident(name)} AS SELECT * FROM {view}"
        )
        conn.unregister(view)
    conn.execute(f"USE {quote_ident(default_namespace)}")
    return conn


class DuckDBIcebergAdapter:
    engine = "duckdb"
    capabilities = Capabilities(
        branch_write=True, publish=True, rollback=True, expire=True, orphan_cleanup=True
    )

    def __init__(self, catalog: LakehouseCatalog) -> None:
        self.catalog = catalog

    # -- reads -----------------------------------------------------------------------------------

    def _qualified(self, table: str) -> str:
        return self.catalog.identifier(table)

    def _load(self, table: str) -> Table:
        try:
            return self.catalog.table(table)
        except NoSuchTableError as err:
            raise ExecutionError(f"table {table} does not exist") from err

    def current_snapshot_id(self, table: str) -> int | None:
        snapshot = self._load(table).current_snapshot()
        return snapshot.snapshot_id if snapshot else None

    def snapshots(self, table: str) -> list[SnapshotInfo]:
        return self.catalog.snapshots(table)

    def read(self, table: str, snapshot_id: int | None = None) -> pa.Table:
        self._load(table)
        return self.catalog.read(table, snapshot_id)

    def query(self, sql: str, tables: Sequence[str], dialect: str = DEFAULT_DIALECT) -> pa.Table:
        statements = self._statements(sql, dialect, allowed=_READ_ONLY)
        loaded = {self._qualified(t): self.read(t) for t in tables}
        conn = _connect(loaded, self.catalog.namespace)
        try:
            result = None
            for statement in statements:
                result = self._run(conn, statement)
            return result if result is not None else pa.table({})
        finally:
            conn.close()

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
        try:
            iceberg.manage_snapshots().create_branch(base.snapshot_id, branch).commit()
        except CommitFailedException as err:
            raise ExecutionError(f"could not create branch on {table}: {err}") from err
        return BranchHandle(self._qualified(table), branch, base.snapshot_id, base.snapshot_id)

    def _branch_head(self, iceberg: Table, branch: BranchHandle) -> int:
        ref = iceberg.refs().get(branch.name)
        return ref.snapshot_id if ref else branch.base_snapshot_id

    def apply_sql(
        self,
        branch: BranchHandle,
        sql: str,
        dialect: str = DEFAULT_DIALECT,
        read_tables: Sequence[str] = (),
    ) -> None:
        statements = self._statements(sql, dialect, allowed=(*_RUNNABLE, *_READ_ONLY))
        iceberg = self._load(branch.table)
        head = self._branch_head(iceberg, branch)
        before = self.catalog.read(branch.table, head)

        loaded = {
            self._qualified(t): self.read(t)
            for t in read_tables
            if self._qualified(t) != branch.table
        }
        loaded[branch.table] = before
        conn = _connect(loaded, self.catalog.namespace)
        try:
            for statement in statements:
                self._run(conn, statement)
            namespace, _, name = branch.table.rpartition(".")
            after = conn.sql(
                f"SELECT * FROM {quote_ident(namespace)}.{quote_ident(name)}"
            ).to_arrow_table()
        except duckdb.Error as err:
            raise ExecutionError(f"DuckDB rejected the statement: {err}") from err
        finally:
            conn.close()

        target = iceberg.schema().as_arrow()
        try:
            after = after.cast(target)
            unchanged = after.equals(before.cast(target), check_metadata=False)
        except (pa.ArrowException, ValueError) as err:
            raise ExecutionError(f"result does not fit the table schema: {err}") from err
        if unchanged:
            return
        try:
            iceberg.overwrite(after, branch=branch.name)
        except Exception as err:  # PyIceberg surfaces I/O, schema and commit errors alike
            raise ExecutionError(f"could not write branch {branch.name}: {err}") from err

    def diff(self, branch: BranchHandle, key: Sequence[str] = ()) -> BranchDiff:
        iceberg = self._load(branch.table)
        head = self._branch_head(iceberg, branch)  # read once: the data and the id must agree
        before = self.catalog.read(branch.table, branch.base_snapshot_id)
        after = self.catalog.read(branch.table, head)
        return BranchDiff(
            before,
            after,
            compute_table_impact(branch.table, before, after, key),
            replace(branch, head_snapshot_id=head),
        )

    def publish(self, branch: BranchHandle, expected_head: int) -> PublishResult:
        iceberg = self._load(branch.table)
        main_id = self._current_id(iceberg)
        if main_id != branch.base_snapshot_id:
            raise BranchDiverged(
                f"main of {branch.table} moved from snapshot {branch.base_snapshot_id} to "
                f"{main_id} since the branch was created"
            )
        if expected_head == branch.base_snapshot_id:
            return PublishResult(branch.table, main_id, expected_head, changed=False)
        if branch.base_snapshot_id not in self._ancestry(iceberg, expected_head):
            raise ExecutionError(
                f"snapshot {expected_head} does not descend from the base snapshot "
                f"{branch.base_snapshot_id} of {branch.table}"
            )
        try:
            iceberg.manage_snapshots().set_current_snapshot(snapshot_id=expected_head).commit()
        except CommitFailedException as err:
            raise BranchDiverged(f"main of {branch.table} changed during publish: {err}") from err
        now = self._current_id(self._load(branch.table))
        if now != expected_head:
            raise PublishMismatch(
                f"main of {branch.table} is at snapshot {now} after publishing {expected_head}",
                now,
            )
        return PublishResult(branch.table, main_id, expected_head, changed=True)

    def drop_branch(self, branch: BranchHandle) -> None:
        if branch.name == MAIN:
            raise ExecutionError("refusing to drop main")
        iceberg = self._load(branch.table)
        if branch.name in iceberg.refs():
            iceberg.manage_snapshots().remove_branch(branch.name).commit()

    def rollback(self, table: str, snapshot_id: int, expected_current: int | None = None) -> None:
        # One load, one commit: PyIceberg pins its main-ref assertion to this very Table object,
        # so a writer that commits after the check below makes the commit fail instead of being
        # silently discarded.
        iceberg = self._load(table)
        if expected_current is not None and self._current_id(iceberg) != expected_current:
            raise BranchDiverged(
                f"main of {table} is at {self._current_id(iceberg)}, expected {expected_current}"
            )
        try:
            iceberg.manage_snapshots().rollback_to_snapshot(snapshot_id).commit()
        except CommitFailedException as err:
            raise BranchDiverged(f"main of {table} changed during rollback: {err}") from err
        except ValueError as err:
            raise ExecutionError(str(err)) from err

    # -- maintenance -----------------------------------------------------------------------------

    def plan_maintenance(self, action: MaintenanceAction) -> MaintenancePlan:
        if isinstance(action, ExpireSnapshots):
            return self._plan_expire(action)
        if isinstance(action, RemoveOrphanFiles):
            return self._plan_orphans(action)
        if isinstance(action, RollbackToSnapshot):
            return self._plan_rollback(action)
        if isinstance(action, FastForward):
            return self._plan_fast_forward(action)
        if isinstance(action, DropTable):
            raise UnsupportedAction("dropping tables is not executed by this adapter")
        raise UnsupportedAction(f"unsupported maintenance action {type(action).__name__}")

    def apply_maintenance(
        self, action: MaintenanceAction, plan: MaintenancePlan
    ) -> MaintenanceResult:
        iceberg = self._load(plan.table)
        before = self._current_id(iceberg)
        if plan.kind == "expire_snapshots":
            return self._apply_expire(iceberg, plan, before)
        if plan.kind == "remove_orphan_files":
            if not isinstance(action, RemoveOrphanFiles) or action.older_than is None:
                raise ExecutionError("remove_orphan_files needs an explicit older_than")
            self._ensure_exclusive_location(iceberg)
            try:
                removed = delete_orphans(iceberg, plan.orphan_files, action.older_than)
            except OrphanSafetyError as err:
                raise ExecutionError(str(err)) from err
            return MaintenanceResult(
                plan, before, before, deleted_files=tuple(f.location for f in removed)
            )
        if before != plan.current_snapshot_id:
            raise ExecutionError(
                f"{plan.table} moved from snapshot {plan.current_snapshot_id} to {before} "
                "since the plan was made"
            )
        if plan.target_snapshot_id is None:
            raise ExecutionError("plan has no target snapshot")
        manage = iceberg.manage_snapshots()
        if isinstance(action, RollbackToSnapshot) and action.ref is None:
            manage.rollback_to_snapshot(plan.target_snapshot_id)
        else:
            manage.set_current_snapshot(snapshot_id=plan.target_snapshot_id)
        try:
            manage.commit()
        except (ValueError, CommitFailedException) as err:
            raise ExecutionError(str(err)) from err
        return MaintenanceResult(plan, before, self._current_id(self._load(plan.table)))

    @staticmethod
    def _require_publishable_ref(name: str) -> None:
        """Audit branches may hold unverified or unapproved changes; main must never point there."""
        if name.startswith(AUDIT_BRANCH_PREFIX):
            raise ExecutionError(
                f"ref {name!r} is an internal audit branch and cannot be a publish target"
            )

    def _ensure_exclusive_location(self, iceberg: Table) -> None:
        """Refuse orphan cleanup when any other table's location overlaps this one's.

        Files of a table nested inside (or containing) this location are unreferenced by *this*
        table's metadata, so listing the directory would call them orphans.
        """
        catalog = self.catalog.catalog
        own = tuple(iceberg.name())
        for namespace in catalog.list_namespaces():
            for identifier in catalog.list_tables(namespace):
                if tuple(identifier) == own:
                    continue
                try:
                    other = catalog.load_table(identifier).location()
                except Exception as err:  # an unreadable table might own files here
                    raise ExecutionError(
                        f"cannot verify that table {'.'.join(identifier)} does not overlap "
                        f"{'.'.join(own)}: {err}"
                    ) from err
                if locations_overlap(iceberg.location(), other):
                    raise ExecutionError(
                        f"orphan cleanup refused: location of {'.'.join(identifier)} ({other}) "
                        f"overlaps {iceberg.location()}"
                    )

    @staticmethod
    def _current_id(iceberg: Table) -> int | None:
        current = iceberg.current_snapshot()
        return current.snapshot_id if current else None

    @staticmethod
    def _ancestry(iceberg: Table, snapshot_id: int | None) -> list[int]:
        """Snapshot ids from `snapshot_id` back through parents (newest first)."""
        chain: list[int] = []
        current = iceberg.snapshot_by_id(snapshot_id) if snapshot_id is not None else None
        while current is not None and current.snapshot_id not in chain:
            chain.append(current.snapshot_id)
            parent = current.parent_snapshot_id
            current = iceberg.snapshot_by_id(parent) if parent is not None else None
        return chain

    def _plan_expire(self, action: ExpireSnapshots) -> MaintenancePlan:
        iceberg = self._load(action.table)
        snapshots = {s.snapshot_id: s for s in iceberg.snapshots()}
        if action.older_than is None and not action.snapshot_ids:
            raise ExecutionError("expire_snapshots needs older_than or explicit snapshot_ids")
        unknown = [i for i in action.snapshot_ids if i not in snapshots]
        if unknown:
            raise ExecutionError(f"unknown snapshot id(s): {unknown}")

        protected = {ref.snapshot_id for ref in iceberg.refs().values()}
        lineage = self._ancestry(iceberg, self._current_id(iceberg))
        protected.update(lineage[: max(action.retain_last or 1, 1)])

        candidates = set(action.snapshot_ids)
        if action.older_than is not None:
            cutoff = _millis(action.older_than)
            candidates.update(i for i, s in snapshots.items() if s.timestamp_ms < cutoff)
        doomed = sorted(candidates - protected, key=lambda i: snapshots[i].timestamp_ms)
        return MaintenancePlan(
            kind="expire_snapshots",
            table=self._qualified(action.table),
            irreversible=True,
            expired=tuple(PlannedSnapshot(i, snapshots[i].timestamp_ms) for i in doomed),
            remaining_snapshots=len(snapshots) - len(doomed),
            current_snapshot_id=self._current_id(iceberg),
        )

    def _apply_expire(
        self, iceberg: Table, plan: MaintenancePlan, before: int | None
    ) -> MaintenanceResult:
        existing = {s.snapshot_id for s in iceberg.snapshots()}
        protected = {ref.snapshot_id for ref in iceberg.refs().values()}
        ids = [s.snapshot_id for s in plan.expired if s.snapshot_id in existing - protected]
        if ids:
            iceberg.maintenance.expire_snapshots().by_ids(ids).commit()
        return MaintenanceResult(plan, before, before, expired_snapshot_ids=tuple(ids))

    def _plan_orphans(self, action: RemoveOrphanFiles) -> MaintenancePlan:
        if action.older_than is None:
            raise ExecutionError("remove_orphan_files needs an explicit older_than")
        iceberg = self._load(action.table)
        self._ensure_exclusive_location(iceberg)
        try:
            orphans = find_orphans(iceberg, action.older_than)
        except OrphanSafetyError as err:
            raise ExecutionError(str(err)) from err
        return MaintenancePlan(
            kind="remove_orphan_files",
            table=self._qualified(action.table),
            irreversible=True,
            orphan_files=tuple(orphans),
            remaining_snapshots=len(iceberg.snapshots()),
            current_snapshot_id=self._current_id(iceberg),
        )

    def _plan_rollback(self, action: RollbackToSnapshot) -> MaintenancePlan:
        iceberg = self._load(action.table)
        given = [x for x in (action.snapshot_id, action.as_of, action.ref) if x is not None]
        if len(given) != 1:
            raise ExecutionError("rollback needs exactly one of snapshot_id, as_of, or ref")
        current = self._current_id(iceberg)
        warnings: tuple[str, ...] = ()
        if action.ref is not None:
            self._require_publishable_ref(action.ref)
            ref = iceberg.refs().get(action.ref)
            if ref is None:
                raise ExecutionError(f"unknown ref {action.ref!r}")
            target = ref.snapshot_id
            if target not in self._ancestry(iceberg, current):
                warnings = (
                    f"ref {action.ref!r} is not part of main's history; moving main there "
                    "publishes content main never contained",
                )
        elif action.snapshot_id is not None:
            if iceberg.snapshot_by_id(action.snapshot_id) is None:
                raise ExecutionError(
                    f"Cannot roll back to unknown snapshot id: {action.snapshot_id}"
                )
            target = action.snapshot_id
        else:
            if action.as_of is None:
                raise ExecutionError("rollback needs exactly one of snapshot_id, as_of, or ref")
            cutoff = _millis(action.as_of)
            eligible = [
                i
                for i in self._ancestry(iceberg, current)
                if (s := iceberg.snapshot_by_id(i)) is not None and s.timestamp_ms <= cutoff
            ]
            if not eligible:
                raise ExecutionError(f"no snapshot of {action.table} at or before {action.as_of}")
            target = eligible[0]
        if action.ref is None and target not in self._ancestry(iceberg, current):
            raise ExecutionError(
                f"Cannot roll back to snapshot, not an ancestor of the current state: {target}"
            )
        return MaintenancePlan(
            kind="rollback",
            table=self._qualified(action.table),
            irreversible=False,
            remaining_snapshots=len(iceberg.snapshots()),
            current_snapshot_id=current,
            target_snapshot_id=target,
            warnings=warnings,
            requires_approval=bool(warnings),
        )

    def _plan_fast_forward(self, action: FastForward) -> MaintenancePlan:
        if action.branch != MAIN:
            raise UnsupportedAction("only fast-forwarding main is supported")
        iceberg = self._load(action.table)
        self._require_publishable_ref(action.to)
        ref = iceberg.refs().get(action.to)
        if ref is None:
            raise ExecutionError(f"unknown ref {action.to!r}")
        current = self._current_id(iceberg)
        if current not in self._ancestry(iceberg, ref.snapshot_id):
            raise ExecutionError(f"main is not an ancestor of {action.to!r}; cannot fast-forward")
        return MaintenancePlan(
            kind="fast_forward",
            table=self._qualified(action.table),
            irreversible=False,
            remaining_snapshots=len(iceberg.snapshots()),
            current_snapshot_id=current,
            target_snapshot_id=ref.snapshot_id,
        )

    # -- helpers ---------------------------------------------------------------------------------

    @staticmethod
    def _statements(sql: str, dialect: str, allowed: tuple[type[exp.Expression], ...]) -> list[str]:
        """Parse `sql` and return DuckDB text for each statement, rejecting disallowed kinds."""
        try:
            parsed = parse(sql, dialect)
        except SqlParseError as err:
            raise ExecutionError(str(err)) from err
        statements = []
        for statement in parsed:
            explain = read_only_explain(statement.ast, dialect)
            if explain is not None:
                statements.append(explain.render(explain.statement.ast.sql(dialect="duckdb")))
                continue
            if not isinstance(statement.ast, allowed):
                raise UnsupportedAction(
                    f"{statement.kind} cannot be executed here; allowed: "
                    f"{', '.join(t.__name__ for t in allowed)}"
                )
            statements.append(statement.ast.sql(dialect="duckdb"))
        return statements

    @staticmethod
    def _run(conn: duckdb.DuckDBPyConnection, statement: str) -> pa.Table | None:
        try:
            relation = conn.sql(statement)
            return relation.to_arrow_table() if relation is not None else None
        except duckdb.Error as err:
            raise ExecutionError(f"DuckDB rejected the statement: {err}") from err


def read_only_explain(ast: exp.Expression, dialect: str):
    """The EXPLAIN wrapping a read-only query, or None. EXPLAIN of anything else is refused."""
    try:
        explain = explained(ast, dialect)
    except SqlParseError as err:
        raise ExecutionError(str(err)) from err
    if explain is None:
        return None
    if not isinstance(explain.statement.ast, _READ_ONLY):
        raise UnsupportedAction("EXPLAIN is only supported for read-only queries")
    return explain
