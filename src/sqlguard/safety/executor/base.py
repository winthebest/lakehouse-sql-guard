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
from sqlguard.safety.actions import MaintenanceAction
from sqlguard.safety.executor.orphan_files import OrphanFile
from sqlguard.safety.impact import TableImpact


class ExecutionError(RuntimeError):
    """The engine could not run the requested change. Nothing was published."""


class UnsupportedAction(ExecutionError):
    """The adapter cannot execute this kind of action."""


class BranchDiverged(ExecutionError):
    """Main moved since the branch was created, so publishing would overwrite someone's work."""


class PublishMismatch(ExecutionError):
    """Main was moved by the publish commit but does not sit on the snapshot that was approved.

    Unlike other execution errors this one is raised after main changed, so the caller must
    restore main (`actual_snapshot_id` is where it is now).
    """

    def __init__(self, message: str, actual_snapshot_id: int | None) -> None:
        super().__init__(message)
        self.actual_snapshot_id = actual_snapshot_id


@dataclass(frozen=True)
class Capabilities:
    """What an executor can do; every field defaults to "no" so an engine opts in explicitly."""

    branch_write: bool = False  # run DML on an isolated branch (needed to rehearse a write)
    publish: bool = False  # move main to a branch snapshot
    rollback: bool = False  # point main back at an earlier snapshot
    expire: bool = False  # expire snapshots
    orphan_cleanup: bool = False  # remove orphan files
    changelog: bool = False  # row-level change feed between snapshots

    @classmethod
    def full(cls) -> Capabilities:
        return cls(True, True, True, True, True, True)


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

        Raises BranchDiverged if main is no longer at the base (or moves during the commit), and
        PublishMismatch if main is not on `expected_head` once the commit returns. Whatever the
        branch has been given since `expected_head` was recorded is not published.
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
