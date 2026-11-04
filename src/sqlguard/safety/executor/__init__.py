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
    PublishStateUnknown,
    UnsupportedAction,
)
from sqlguard.safety.executor.composite import CompositeAdapter
from sqlguard.safety.executor.duckdb_iceberg import DuckDBIcebergAdapter
from sqlguard.safety.executor.orphan_files import (
    OrphanFile,
    OrphanSafetyError,
    delete_orphans,
    find_orphans,
    referenced_paths,
)
from sqlguard.safety.executor.spark_iceberg import SparkIcebergAdapter
from sqlguard.safety.executor.trino_iceberg import (
    MaintenanceNeedsAttention,
    RollbackNeedsAttention,
    TrinoIcebergAdapter,
)

__all__ = [
    "BranchDiff",
    "BranchDiverged",
    "BranchHandle",
    "Capabilities",
    "CompositeAdapter",
    "DuckDBIcebergAdapter",
    "ExecutionError",
    "ExecutorAdapter",
    "MaintenanceNeedsAttention",
    "MaintenancePlan",
    "MaintenanceResult",
    "OrphanFile",
    "OrphanSafetyError",
    "PlannedSnapshot",
    "PublishMismatch",
    "PublishResult",
    "PublishStateUnknown",
    "RollbackNeedsAttention",
    "SparkIcebergAdapter",
    "TrinoIcebergAdapter",
    "UnsupportedAction",
    "delete_orphans",
    "find_orphans",
    "referenced_paths",
]
