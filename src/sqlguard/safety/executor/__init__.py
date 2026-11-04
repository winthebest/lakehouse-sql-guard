"""Execution engines the Guard can run changes on (see `base.ExecutorAdapter`)."""

from sqlguard.safety.executor.base import (
    BranchDiff,
    BranchDiverged,
    BranchHandle,
    Capabilities,
    ExecutionError,
    ExecutorAdapter,
    MaintenancePlan,
    MaintenanceResult,
    PlannedSnapshot,
    PublishMismatch,
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
from sqlguard.safety.executor.spark_iceberg import SparkIcebergAdapter

__all__ = [
    "BranchDiff",
    "BranchDiverged",
    "BranchHandle",
    "Capabilities",
    "DuckDBIcebergAdapter",
    "ExecutionError",
    "ExecutorAdapter",
    "MaintenancePlan",
    "MaintenanceResult",
    "OrphanFile",
    "OrphanSafetyError",
    "PlannedSnapshot",
    "PublishMismatch",
    "PublishResult",
    "SparkIcebergAdapter",
    "UnsupportedAction",
    "delete_orphans",
    "find_orphans",
    "referenced_paths",
]
