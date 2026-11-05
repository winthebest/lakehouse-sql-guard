"""The contract between the Guard and an execution engine.

An adapter knows how to run changes in isolation (an Iceberg branch), measure them, publish them
to main, and perform table maintenance. The Guard never touches a table except through it, so a
Spark adapter can replace the DuckDB one without changing the pipeline.

An adapter declares what it can do in `capabilities`; the Guard refuses actions the engine cannot
carry out safely (for example a write on an engine with no isolated branch writes) instead of
trying them.

Publishing is by snapshot id only: `publish(branch, expected_head)` moves main to exactly the
snapshot that was measured and approved, never to "whatever the branch points at now".

Adapters work on table names as the policy sees them: `ns.name`, or a bare `name` in the
catalog's namespace.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

import pyarrow as pa

from sqlguard.core.catalog import SnapshotInfo
from sqlguard.core.parsing import DEFAULT_DIALECT
from sqlguard.safety.actions import ColumnChange, MaintenanceAction
from sqlguard.safety.executor.orphan_files import OrphanFile
from sqlguard.safety.impact import TableImpact


class ExecutionError(RuntimeError):
    """The engine could not run the requested change. Nothing was published."""


class UnsupportedAction(ExecutionError):
    """The adapter cannot execute this kind of action."""


class SchemaChangeRefused(ExecutionError):
    """A column change the table must not take (a key column, a name clash, a no-op ...).

    The Guard reports it as a denial with this reason, not as an engine failure.
    """


class BranchDiverged(ExecutionError):
    """Main moved since the branch was created, so publishing would overwrite someone's work."""


class PublishMismatch(ExecutionError):
    """The publish commit went through, but main no longer has the approved snapshot in its history.

    Someone replaced main afterwards, so the approved change is not live. Nothing is rolled
    back: main holds another writer's work and needs a person to look. `actual_snapshot_id` is
    where main is now.
    """

    def __init__(self, message: str, actual_snapshot_id: int | None) -> None:
        super().__init__(message)
        self.actual_snapshot_id = actual_snapshot_id


class PublishStateUnknown(ExecutionError):
    """The publish commit may or may not have landed and main could not be read back to find out.

    Main might hold the approved change; the caller must not report it as "not published".
    """


@dataclass(frozen=True)
class Capabilities:
    """What an executor can do; every field defaults to "no" so an engine opts in explicitly."""

    branch_write: bool = False  # run DML on an isolated branch (needed to rehearse a write)
    publish: bool = False  # move main to a branch snapshot
    rollback: bool = False  # point main back at an earlier snapshot
    expire: bool = False  # expire snapshots
    orphan_cleanup: bool = False  # remove orphan files
    changelog: bool = False  # row-level change feed between snapshots
    # apply a column drop or rename as one catalog commit that fails if main or the schema moved
    schema_change: bool = False

    @classmethod
    def full(cls) -> Capabilities:
        return cls(True, True, True, True, True, True, True)


@dataclass(frozen=True)
class BranchHandle:
    """An isolated audit branch of one table and the main snapshot it was cut from.

    `head_snapshot_id` is the branch head this handle vouches for. A new branch's head is its
    base; `ExecutorAdapter.diff` returns the handle with the head it measured, and that is the
    snapshot to publish.
    """

    table: str
    name: str
    base_snapshot_id: int
    head_snapshot_id: int | None = None

    def __post_init__(self) -> None:
        if self.head_snapshot_id is None:
            object.__setattr__(self, "head_snapshot_id", self.base_snapshot_id)

    @property
    def head(self) -> int:
        """The recorded head (never None once constructed)."""
        return self.base_snapshot_id if self.head_snapshot_id is None else self.head_snapshot_id


@dataclass(frozen=True)
class BranchDiff:
    """Main-at-branch-creation versus the branch head, as data and as measured impact.

    `branch` is the handle recording the head that `after` was read from.
    """

    before: pa.Table
    after: pa.Table
    impact: TableImpact
    branch: BranchHandle


@dataclass(frozen=True)
class PublishResult:
    table: str
    previous_snapshot_id: int
    snapshot_id: int
    changed: bool  # False when the branch made no change, so main did not move


@dataclass(frozen=True)
class PlannedSnapshot:
    snapshot_id: int
    timestamp_ms: int


@dataclass(frozen=True)
class MaintenancePlan:
    """Read-only preview of a maintenance action: what exactly it would remove or move."""

    kind: str  # expire_snapshots | remove_orphan_files | rollback | fast_forward
    table: str
    irreversible: bool
    expired: tuple[PlannedSnapshot, ...] = ()
    remaining_snapshots: int = 0
    orphan_files: tuple[OrphanFile, ...] = ()
    current_snapshot_id: int | None = None
    target_snapshot_id: int | None = None
    # extra reasons an approver must see, and whether approval is needed whatever the policy says
    warnings: tuple[str, ...] = ()
    requires_approval: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "table": self.table,
            "irreversible": self.irreversible,
            "expired_snapshot_ids": [s.snapshot_id for s in self.expired],
            "remaining_snapshots": self.remaining_snapshots,
            "orphan_file_count": len(self.orphan_files),
            "orphan_bytes": sum(f.size for f in self.orphan_files),
            "orphan_files": [f.location for f in self.orphan_files],
            "current_snapshot_id": self.current_snapshot_id,
            "target_snapshot_id": self.target_snapshot_id,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class MaintenanceResult:
    plan: MaintenancePlan
    snapshot_before: int | None
    snapshot_after: int | None
    expired_snapshot_ids: tuple[int, ...] = ()
    deleted_files: tuple[str, ...] = ()


def schema_line(table: str, changes: Sequence[str]) -> str:
    return (
        f"alter_table on {table}: {', '.join(changes)} (not rehearsed; shows the schema, not rows)"
    )


@dataclass(frozen=True)
class ColumnInfo:
    """A column as the schema holds it (the field id tells a renamed column from a new one)."""

    field_id: int
    name: str
    type: str
    required: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "field_id": self.field_id,
            "name": self.name,
            "type": self.type,
            "required": self.required,
        }


@dataclass(frozen=True)
class SchemaPlan:
    """A read-only preview of a column change: the table's columns now and after the change.

    A schema change cannot be rehearsed on a branch (Iceberg keeps one schema per table, not per
    branch), so nothing about it has been run: `after` is what the engine computed the new schema
    would be, and the apply step checks that the table ends up exactly there. It is tied to the
    state it was planned on: the snapshot main was on and the id of the schema.
    """

    table: str
    changes: tuple[ColumnChange, ...]
    schema_id: int
    base_snapshot_id: int | None
    before: tuple[ColumnInfo, ...]
    after: tuple[ColumnInfo, ...]

    @property
    def removed(self) -> tuple[str, ...]:
        kept = {c.field_id for c in self.after}
        return tuple(c.name for c in self.before if c.field_id not in kept)

    @property
    def renamed(self) -> tuple[tuple[str, str], ...]:
        names = {c.field_id: c.name for c in self.after}
        return tuple(
            (c.name, names[c.field_id])
            for c in self.before
            if c.field_id in names and names[c.field_id] != c.name
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "table": self.table,
            "changes": [c.describe() for c in self.changes],
            "schema_id": self.schema_id,
            "base_snapshot_id": self.base_snapshot_id,
            "columns_before": [c.to_dict() for c in self.before],
            "columns_after": [c.to_dict() for c in self.after],
            "removed": list(self.removed),
            "renamed": [list(pair) for pair in self.renamed],
            "rehearsed": False,
        }

    def summary(self) -> str:
        """One line for a person or an agent: what changes, and that it was not rehearsed."""
        return schema_line(self.table, [c.describe() for c in self.changes])


class ExecutorAdapter(Protocol):
    engine: str  # name used in reasons shown to users, e.g. "duckdb"
    capabilities: Capabilities

    def current_snapshot_id(self, table: str) -> int | None: ...

    def snapshots(self, table: str) -> list[SnapshotInfo]: ...

    def read(self, table: str, snapshot_id: int | None = None) -> pa.Table: ...

    def query(self, sql: str, tables: Sequence[str], dialect: str = DEFAULT_DIALECT) -> pa.Table:
        """Run read-only SQL against main and return the last statement's result."""
        ...

    def create_branch(self, table: str, name: str | None = None) -> BranchHandle: ...

    def apply_sql(
        self,
        branch: BranchHandle,
        sql: str,
        dialect: str = DEFAULT_DIALECT,
        read_tables: Sequence[str] = (),
    ) -> None:
        """Run DML on the branch only; `read_tables` are other tables it reads from main."""
        ...

    def diff(self, branch: BranchHandle, key: Sequence[str] = ()) -> BranchDiff:
        """Measure the branch head; `BranchDiff.branch` records which snapshot that was."""
        ...

    def publish(self, branch: BranchHandle, expected_head: int) -> PublishResult:
        """Move main to snapshot `expected_head`, which must descend from the branch's base.

        Raises BranchDiverged if main is no longer at the base (or moves during the commit).
        After the commit main is read back: it must be on `expected_head`, or have it in its
        history (other writers committed on top; that is a success). If it does not, raises
        PublishMismatch; if the outcome of the commit cannot be determined, PublishStateUnknown.
        Whatever the branch has been given since `expected_head` was recorded is not published.
        """
        ...

    def drop_branch(self, branch: BranchHandle) -> None: ...

    def rollback(self, table: str, snapshot_id: int, expected_current: int | None = None) -> None:
        """Point main back at an earlier snapshot of its own history.

        When `expected_current` is given, raises BranchDiverged unless main is exactly there at
        commit time, so a concurrent writer's commit is never discarded.
        """
        ...

    def plan_maintenance(self, action: MaintenanceAction) -> MaintenancePlan: ...

    def apply_maintenance(
        self, action: MaintenanceAction, plan: MaintenancePlan
    ) -> MaintenanceResult:
        """Execute exactly the items of `plan` that are still valid."""
        ...

    def plan_schema_change(self, table: str, changes: Sequence[ColumnChange]) -> SchemaPlan:
        """Read-only: the schema the table would have after `changes`. Raises ExecutionError when
        a change cannot be applied (no such column, the new name is taken, a key column ...)."""
        ...

    def apply_schema_change(self, plan: SchemaPlan) -> None:
        """Apply `plan` in one catalog commit that fails if main or the schema is not as planned.

        Raises BranchDiverged when it is not (nothing changed), PublishStateUnknown when the
        commit's outcome cannot be established, and PublishMismatch when the table ends up with
        a schema other than the planned one.
        """
        ...
