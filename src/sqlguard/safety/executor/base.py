"""The contract between the Guard and an execution engine.

An adapter knows how to run changes in isolation (an Iceberg branch), measure them, publish them
to main, and perform table maintenance. The Guard never touches a table except through it, so a
Spark adapter can replace the DuckDB one without changing the pipeline.

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


@dataclass(frozen=True)
class BranchHandle:
    """An isolated audit branch of one table and the main snapshot it was cut from."""

    table: str
    name: str
    base_snapshot_id: int


@dataclass(frozen=True)
class BranchDiff:
    """Main-at-branch-creation versus the branch head, as data and as measured impact."""

    before: pa.Table
    after: pa.Table
    impact: TableImpact


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

    def diff(self, branch: BranchHandle, key: Sequence[str] = ()) -> BranchDiff: ...

    def publish(self, branch: BranchHandle) -> PublishResult:
        """Move main to the branch head; raises BranchDiverged if main moved meanwhile."""
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
