"""Static safety layer: classify agent actions by reversibility and decide allow/ask/deny."""

from sqlguard.safety.actions import (
    Action,
    DropTable,
    ExpireSnapshots,
    FastForward,
    MaintenanceAction,
    RemoveOrphanFiles,
    RollbackToSnapshot,
    SqlAction,
    UnsupportedCall,
    parse_spark_call,
)
from sqlguard.safety.classifier import (
    Classification,
    Impact,
    ReversibilityClass,
    StatementClass,
    classify,
)
from sqlguard.safety.policy import (
    Policy,
    PolicyDecision,
    PolicyError,
    RetentionRule,
    StatementDecision,
    TaskScope,
    Thresholds,
)

__all__ = [
    "Action",
    "Classification",
    "DropTable",
    "ExpireSnapshots",
    "FastForward",
    "Impact",
    "MaintenanceAction",
    "Policy",
    "PolicyDecision",
    "PolicyError",
    "RemoveOrphanFiles",
    "RetentionRule",
    "ReversibilityClass",
    "RollbackToSnapshot",
    "SqlAction",
    "StatementClass",
    "StatementDecision",
    "TaskScope",
    "Thresholds",
    "UnsupportedCall",
    "classify",
    "parse_spark_call",
]
