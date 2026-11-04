"""One executor made of two: an engine that reads and verifies, an engine that writes.

The Guard holds a single adapter. `CompositeAdapter(read=Trino, write=PyIceberg|Spark)` routes
each call by role:

  * read role: `current_snapshot_id`, `snapshots`, `read`, `query` and `diff`. The data an approver
    is shown and the impact numbers are produced by the read engine, from the branch head it sees.
  * write role: `create_branch`, `apply_sql`, `drop_branch`, `publish`, `rollback` and all
    maintenance. These are the operations that change the table, so they go to the engine that
    can do them with the catalog's compare-and-swap.

`capabilities` is the union by role: the write engine decides whether the combination can
rehearse a write, publish, roll back, expire or clean orphans; the read engine adds the changelog
(and anything else only it declares). Publishing is still by snapshot id: `diff` returns the head
the read engine measured and `publish(branch, expected_head)` moves main to exactly that.
Both engines must serve the same Iceberg catalog.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import fields

import pyarrow as pa

from sqlguard.core.catalog import SnapshotInfo
from sqlguard.core.parsing import DEFAULT_DIALECT
from sqlguard.safety.actions import MaintenanceAction
from sqlguard.safety.executor.base import (
    BranchDiff,
    BranchHandle,
    Capabilities,
    ExecutorAdapter,
    MaintenancePlan,
    MaintenanceResult,
    PublishResult,
    UnsupportedAction,
)

_READ_ROLE = frozenset({"changelog"})


def combined_capabilities(read: Capabilities, write: Capabilities) -> Capabilities:
    """Write-role abilities come from `write`; read-role ones from either engine."""
    merged = {}
    for field in fields(Capabilities):
        name = field.name
        if name in _READ_ROLE:
            merged[name] = getattr(read, name) or getattr(write, name)
        else:
            merged[name] = getattr(write, name)
    return Capabilities(**merged)


class CompositeAdapter:
    def __init__(self, read: ExecutorAdapter, write: ExecutorAdapter) -> None:
        if read is write:
            raise ValueError("a composite needs two engines; use the adapter itself")
        self.read_engine = read
        self.write_engine = write
        self.engine = f"{read.engine}+{write.engine}"
        self.capabilities = combined_capabilities(
            getattr(read, "capabilities", None) or Capabilities(),
            getattr(write, "capabilities", None) or Capabilities(),
        )

    # -- read role -------------------------------------------------------------------------------

    def current_snapshot_id(self, table: str) -> int | None:
        return self.read_engine.current_snapshot_id(table)

    def snapshots(self, table: str) -> list[SnapshotInfo]:
        return self.read_engine.snapshots(table)

    def read(self, table: str, snapshot_id: int | None = None) -> pa.Table:
        return self.read_engine.read(table, snapshot_id)

    def query(self, sql: str, tables: Sequence[str], dialect: str = DEFAULT_DIALECT) -> pa.Table:
        return self.read_engine.query(sql, tables, dialect)

    def changes(self, table: str, start_snapshot_id: int, end_snapshot_id: int) -> pa.Table:
        """Row changes between two snapshots, from the read engine when it has a change feed."""
        feed = getattr(self.read_engine, "changes", None)
        if feed is None:
            raise UnsupportedAction(f"{self.read_engine.engine} has no change feed")
        return feed(table, start_snapshot_id, end_snapshot_id)

    def diff(self, branch: BranchHandle, key: Sequence[str] = ()) -> BranchDiff:
        return self.read_engine.diff(branch, key)

    # -- write role ------------------------------------------------------------------------------

    def create_branch(self, table: str, name: str | None = None) -> BranchHandle:
        return self.write_engine.create_branch(table, name)

    def apply_sql(
        self,
        branch: BranchHandle,
        sql: str,
        dialect: str = DEFAULT_DIALECT,
        read_tables: Sequence[str] = (),
    ) -> None:
        self.write_engine.apply_sql(branch, sql, dialect, read_tables)

    def publish(self, branch: BranchHandle, expected_head: int) -> PublishResult:
        return self.write_engine.publish(branch, expected_head)

    def drop_branch(self, branch: BranchHandle) -> None:
        self.write_engine.drop_branch(branch)

    def rollback(self, table: str, snapshot_id: int, expected_current: int | None = None) -> None:
        self.write_engine.rollback(table, snapshot_id, expected_current)

    def plan_maintenance(self, action: MaintenanceAction) -> MaintenancePlan:
        return self.write_engine.plan_maintenance(action)

    def apply_maintenance(
        self, action: MaintenanceAction, plan: MaintenancePlan
    ) -> MaintenanceResult:
        return self.write_engine.apply_maintenance(action, plan)


__all__ = ["CompositeAdapter", "combined_capabilities"]
