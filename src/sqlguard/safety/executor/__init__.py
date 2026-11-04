"""Execution engines the Guard can run changes on (see `base.ExecutorAdapter`)."""

from sqlguard.safety.executor.base import (
    BranchDiff,
    BranchDiverged,
    BranchHandle,
    ExecutionError,
    ExecutorAdapter,
    MaintenancePlan,
    MaintenanceResult,
    PlannedSnapshot,
    PublishResult,
    UnsupportedAction,
)
from sqlguard.safety.executor.duckdb_iceberg import DuckDBIcebergAdapter
from sqlguard.safety.executor.orphan_files import (
    OrphanFile,
    OrphanSafetyError,
    delete_orphans,
    find_orphans,
    referenced_paths,
)

__all__ = [
    "BranchDiff",
    "BranchDiverged",
    "BranchHandle",
    "DuckDBIcebergAdapter",
    "ExecutionError",
    "ExecutorAdapter",
    "MaintenancePlan",
    "MaintenanceResult",
    "OrphanFile",
    "OrphanSafetyError",
    "PlannedSnapshot",
    "PublishResult",
    "UnsupportedAction",
    "delete_orphans",
    "find_orphans",
    "referenced_paths",
]
