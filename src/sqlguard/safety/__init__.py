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
from sqlguard.safety.audit import AuditError, AuditLog
from sqlguard.safety.classifier import (
    Classification,
    Impact,
    ReversibilityClass,
    StatementClass,
    classify,
)
from sqlguard.safety.guard import ApprovalRequest, Approver, Guard, GuardResult, Outcome
from sqlguard.safety.impact import ImpactReport, SchemaDiff, TableImpact
from sqlguard.safety.invariants import (
    Invariant,
    InvariantContext,
    InvariantResult,
    InvariantsConfig,
    TableInvariants,
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
    "ApprovalRequest",
    "Approver",
    "AuditError",
    "AuditLog",
    "Classification",
    "DropTable",
    "ExpireSnapshots",
    "FastForward",
    "Guard",
    "GuardResult",
    "Impact",
    "ImpactReport",
    "Invariant",
    "InvariantContext",
    "InvariantResult",
    "InvariantsConfig",
    "MaintenanceAction",
    "Outcome",
    "Policy",
    "PolicyDecision",
    "PolicyError",
    "RemoveOrphanFiles",
    "RetentionRule",
    "ReversibilityClass",
    "RollbackToSnapshot",
    "SchemaDiff",
    "SqlAction",
    "StatementClass",
    "StatementDecision",
    "TableImpact",
    "TableInvariants",
    "TaskScope",
    "Thresholds",
    "UnsupportedCall",
    "classify",
    "parse_spark_call",
]
