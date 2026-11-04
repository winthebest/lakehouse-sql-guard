"""The Guard: one entry point that classifies, decides, rehearses, verifies, and publishes.

    guard = Guard(catalog, policy, audit_path="audit.jsonl")
    result = guard.submit(SqlAction("DELETE FROM orders WHERE id = 7"), scope, approver=ask_human)

Pipeline for every submission:

    classify -> policy decision
      DENY                  -> nothing runs
      ALLOW (reads)         -> query main, return rows
      ALLOW_VIA_BRANCH      -> run on an audit branch, measure impact, check invariants, re-decide
                               with the measured impact, publish only if main has not moved,
                               verify main afterwards and roll back if verification fails
      ASK                   -> nothing runs without an approver; SQL writes are first rehearsed on
                               a branch so the approver sees the measured impact
      IRREVERSIBLE actions  -> need an approver, and the live retention check must pass first

Main is never written directly: a SQL write reaches main only through `publish`, which moves the
main pointer to the already-verified branch head. Every submission appends a completion record
to the audit log (denials and failures included), and anything with side effects first appends
a "started" record. Any unexpected error fails closed (nothing published, branch
dropped) and is reported as `Outcome.ERROR`.

This module is deterministic and contains no LLM.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

import pyarrow as pa
import sqlglot
from sqlglot import exp

from sqlguard.core.catalog import LakehouseCatalog
from sqlguard.core.findings import Decision
from sqlguard.core.parsing import explained
from sqlguard.safety.actions import Action, SqlAction
from sqlguard.safety.audit import AuditError, AuditLog
from sqlguard.safety.classifier import Classification, ReversibilityClass, classify
from sqlguard.safety.executor.base import (
    BranchDiverged,
    BranchHandle,
    ExecutionError,
    ExecutorAdapter,
    MaintenancePlan,
    UnsupportedAction,
)
from sqlguard.safety.executor.duckdb_iceberg import DuckDBIcebergAdapter
from sqlguard.safety.impact import ImpactReport, compute_table_impact
from sqlguard.safety.invariants import (
    Invariant,
    InvariantContext,
    InvariantResult,
    evaluate,
    invariants_for,
)
from sqlguard.safety.policy import Policy, PolicyDecision, RetentionRule, TaskScope

C = ReversibilityClass
_POINTER_MOVES = frozenset({"rollback", "fast_forward"})
_DML_VERBS = frozenset({"INSERT", "UPDATE", "DELETE", "MERGE"})
_AUDIT_FILE_LIST_LIMIT = 100
_ERROR_TEXT_LIMIT = 300
_LITERAL_NODES = tuple(
    getattr(exp, name)
    for name in ("Literal", "HexString", "BitString", "ByteString", "National")
    if hasattr(exp, name)
)
_log = logging.getLogger("sqlguard.audit")


class Outcome(StrEnum):
    """What happened to a submission."""

    DENIED = "denied"  # policy, retention, or a measured limit said no
    AWAITING_APPROVAL = "awaiting_approval"  # ASK and no approver was supplied
    REJECTED = "rejected"  # the approver declined
    READ = "read"  # query ran on main
    PUBLISHED = "published"  # change verified and live on main
    NO_CHANGE = "no_change"  # statement ran but changed no rows; main untouched
    INVARIANT_FAILED = "invariant_failed"  # pre-publish invariant failed; branch dropped
    REFUSED_DIVERGED = "refused_diverged"  # main moved while the branch was being checked
    ROLLED_BACK = "rolled_back"  # published, post-publish check failed, main restored
    ROLLBACK_FAILED = "rollback_failed"  # post-publish check failed and restoring main failed
    ROLLBACK_BLOCKED = "rollback_blocked"  # check failed but main already moved on; left alone
    MAINTENANCE_DONE = "maintenance_done"
    UNSUPPORTED = "unsupported"  # the adapter cannot execute this kind of action
    EXECUTION_FAILED = "execution_failed"  # the engine rejected the statement; nothing published
    ERROR = "error"  # unexpected failure; failed closed


@dataclass(frozen=True)
class ApprovalRequest:
    """Everything an approver needs to decide."""

    action: Action
    classification: Classification
    decision: PolicyDecision
    impact: ImpactReport | None = None  # measured on a branch (SQL writes)
    plan: MaintenancePlan | None = None  # what maintenance would remove or move


# Returns True to approve. An approver that raises is treated as having declined.
Approver = Callable[[ApprovalRequest], bool]


@dataclass(frozen=True)
class GuardResult:
    decision: Decision  # final decision, after the measured impact was taken into account
    outcome: Outcome
    klass: ReversibilityClass
    reasons: tuple[str, ...]
    audit_id: str
    executed: bool  # statements ran (possibly only on a discarded branch)
    published: bool  # main now holds the change (or its pointer was moved on purpose)
    rolled_back: bool = False
    impact: ImpactReport | None = None
    invariants: tuple[InvariantResult, ...] = ()
    plan: MaintenancePlan | None = None
    data: pa.Table | None = None  # rows returned by a read
    snapshots_before: dict[str, int | None] = field(default_factory=dict)
    snapshots_after: dict[str, int | None] = field(default_factory=dict)
    error: str | None = None
    audit_error: str | None = None  # set when the completion record could not be written

    @property
    def needs_attention(self) -> bool:
        """True when main may hold a change that failed verification and was not undone."""
        return self.outcome in _ATTENTION_OUTCOMES

    @property
    def ok(self) -> bool:
        """True when the action did what was asked: it ran to completion and stayed applied."""
        return self.outcome in (
            Outcome.READ,
            Outcome.PUBLISHED,
            Outcome.NO_CHANGE,
            Outcome.MAINTENANCE_DONE,
        )

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly summary (without result rows)."""
        return {
            "decision": self.decision.value,
            "outcome": self.outcome.value,
            "class": self.klass.value,
            "reasons": list(self.reasons),
            "audit_id": self.audit_id,
            "executed": self.executed,
            "published": self.published,
            "rolled_back": self.rolled_back,
            "impact": self.impact.to_dict() if self.impact else None,
            "invariants": [r.to_dict() for r in self.invariants],
            "plan": self.plan.to_dict() if self.plan else None,
            "rows_returned": self.data.num_rows if self.data is not None else None,
            "snapshots_before": self.snapshots_before,
            "snapshots_after": self.snapshots_after,
            "error": self.error,
            "audit_error": self.audit_error,
            "needs_attention": self.needs_attention,
        }


@dataclass
class _Run:
    """Mutable state of one submission; becomes the audit record and the GuardResult."""

    action: Action
    scope: TaskScope | None
    audit_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    approver_id: str | None = None
    classification: Classification | None = None
    decision: Decision = Decision.DENY  # fail closed until a policy decision exists
    klass: ReversibilityClass = ReversibilityClass.UNKNOWN
    reasons: list[str] = field(default_factory=list)
    outcome: Outcome = Outcome.ERROR
    approval: str = "not_required"
    executed: bool = False
    published: bool = False
    rolled_back: bool = False
    impact: ImpactReport | None = None
    invariants: list[InvariantResult] = field(default_factory=list)
    plan: MaintenancePlan | None = None
    maintenance_result: dict[str, Any] | None = None
    data: pa.Table | None = None
    before: dict[str, int | None] = field(default_factory=dict)
    after: dict[str, int | None] = field(default_factory=dict)
    branch: str | None = None
    error: str | None = None

    def set_decision(self, decided: PolicyDecision) -> None:
        self.decision = decided.decision
        self.klass = decided.klass
        # keep the reasons of earlier stages; add only what is new
        self.reasons.extend(r for r in decided.reasons if r not in self.reasons)

    def finish(self, outcome: Outcome, *reasons: str) -> None:
        self.outcome = outcome
        self.reasons.extend(reasons)


_ATTENTION_OUTCOMES = frozenset({Outcome.ROLLBACK_FAILED, Outcome.ROLLBACK_BLOCKED})


class Guard:
    """Gatekeeper for agent actions against one Iceberg catalog.

    Args:
        catalog: the lakehouse the actions run against.
        policy: allow/ask/deny rules, thresholds, retention, and invariants (strict defaults).
        audit_path: JSONL file (or an `AuditLog`) every submission is appended to.
        adapter: execution engine; defaults to `DuckDBIcebergAdapter(catalog)`.
        extra_invariants: code-defined invariants applied to every table in addition to the
            policy's; use `Invariant.phases` to limit one to publish time or to the live check.
        clock: time source for retention checks (injectable for tests).
        redact_sql_literals: store SQL in the audit log with string and number literals replaced
            by `?` (plus a sha256 of the original text). On by default because agent SQL can
            carry personal data or secrets.

    Audit trail: a "started" record is written before the first side effect (branch creation or
    maintenance) and a "completed" record afterwards; both share one `id`. If the completion
    record cannot be written the result is still returned with `audit_error` set and the record
    is sent to the `sqlguard.audit` logger, and the started record still shows what was attempted.
    """

    def __init__(
        self,
        catalog: LakehouseCatalog,
        policy: Policy | None = None,
        *,
        audit_path: str | Path | AuditLog,
        adapter: ExecutorAdapter | None = None,
        extra_invariants: Sequence[Invariant] = (),
        clock: Callable[[], datetime] | None = None,
        redact_sql_literals: bool = True,
    ) -> None:
        self.catalog = catalog
        self.policy = policy or Policy()
        self.audit = audit_path if isinstance(audit_path, AuditLog) else AuditLog(audit_path)
        self.adapter: ExecutorAdapter = adapter or DuckDBIcebergAdapter(catalog)
        self.extra_invariants = tuple(extra_invariants)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._redact = redact_sql_literals

    # -- public API ------------------------------------------------------------------------------

    def submit(
        self,
        action: Action,
        task_scope: TaskScope | None = None,
        approver: Approver | None = None,
    ) -> GuardResult:
        """Run `action` through the pipeline and return what happened.

        Never raises for a denied, failed, or rolled-back action. `AuditError` is raised only
        when the log is unwritable before anything has run (a failure to write the "started"
        record also happens before any side effect); a failure to write the final record is
        reported in `GuardResult.audit_error` instead, because the action may already be live.
        """
        self.audit.ensure_writable()
        run = _Run(action=action, scope=task_scope)
        try:
            self._pipeline(run, approver)
        except AuditError:
            raise
        except UnsupportedAction as err:
            run.error = _clip(str(err))
            run.finish(Outcome.UNSUPPORTED, run.error)
        except ExecutionError as err:
            run.error = _clip(str(err))
            run.finish(Outcome.EXECUTION_FAILED, run.error)
        except Exception as err:  # fail closed on anything unexpected
            run.error = _clip(f"{type(err).__name__}: {err}")
            run.finish(Outcome.ERROR, f"unexpected error: {run.error}")
        except BaseException as err:  # e.g. KeyboardInterrupt: leave a record, then propagate
            run.error = f"interrupted by {type(err).__name__}"
            run.finish(Outcome.ERROR, run.error)
            self._complete(run)
            raise
        return self._complete(run)

    # -- pipeline --------------------------------------------------------------------------------

    def _pipeline(self, run: _Run, approver: Approver | None) -> None:
        now = self._clock()
        classification = classify(run.action, self.catalog)
        run.classification = classification
        decided = self.policy.decide(classification, run.scope, now)
        run.set_decision(decided)

        if decided.decision is Decision.DENY:
            run.finish(Outcome.DENIED)
            return

        maintenance = (
            classification.statements[0].maintenance
            if len(classification.statements) == 1
            else None
        )
        if decided.decision is Decision.ASK and approver is None:
            if maintenance is not None:
                self._preview(run, maintenance)
            run.approval = "missing"
            run.finish(Outcome.AWAITING_APPROVAL, "approval is required and no approver was given")
            return

        if maintenance is not None:
            self._run_maintenance(run, decided, maintenance, approver, now)
        elif classification.klass is C.READ:
            self._run_read(run, decided, approver)
        else:
            self._run_write(run, decided, approver, now)

    # -- approval --------------------------------------------------------------------------------

    def _approved(self, run: _Run, approver: Approver | None, request: ApprovalRequest) -> bool:
        if approver is None:
            run.approval = "missing"
            run.finish(Outcome.AWAITING_APPROVAL, "approval is required and no approver was given")
            return False
        run.approver_id = _approver_id(approver)
        try:
            answer = approver(request)
        except Exception as err:  # an approver that crashes has not approved
            run.approval = "error"
            run.error = _clip(f"approver raised {type(err).__name__}: {err}")
            run.finish(Outcome.REJECTED, "approver failed; treated as rejected")
            return False
        if answer is True:  # exactly True: "yes", 1, or a non-empty object must not approve
            run.approval = "approved"
            return True
        run.approval = "rejected" if answer is False else "invalid_response"
        run.finish(
            Outcome.REJECTED,
            "approver rejected the action"
            if answer is False
            else f"approver returned {type(answer).__name__}, not a bool; treated as rejected",
        )
        return False

    def _request(self, run: _Run, decided: PolicyDecision, **extra: Any) -> ApprovalRequest:
        if run.classification is None:
            raise RuntimeError("approval requested before classification")
        return ApprovalRequest(run.action, run.classification, decided, **extra)

    # -- reads -----------------------------------------------------------------------------------

    def _run_read(self, run: _Run, decided: PolicyDecision, approver: Approver | None) -> None:
        if not isinstance(run.action, SqlAction) or run.classification is None:
            raise RuntimeError("read path requires a classified SQL action")
        if decided.decision is Decision.ASK and not self._approved(
            run, approver, self._request(run, decided)
        ):
            return
        run.data = self.adapter.query(run.action.sql, run.classification.tables, run.action.dialect)
        run.executed = True
        run.finish(Outcome.READ)

    # -- maintenance -----------------------------------------------------------------------------

    def _preview(self, run: _Run, maintenance: Any) -> None:
        """Best-effort read-only plan so a caller who cannot approve still sees the effect."""
        try:
            run.plan = self.adapter.plan_maintenance(maintenance)
        except ExecutionError as err:
            run.reasons.append(f"plan unavailable: {err}")

    def _run_maintenance(
        self,
        run: _Run,
        decided: PolicyDecision,
        maintenance: Any,
        approver: Approver | None,
        now: datetime,
    ) -> None:
        if run.classification is None:
            raise RuntimeError("maintenance requires a classified action")
        table = run.classification.statements[0].write_tables[0]
        plan = self.adapter.plan_maintenance(maintenance)
        run.plan = plan
        run.reasons.extend(w for w in plan.warnings if w not in run.reasons)

        if plan.irreversible:
            problems = _retention_violations(plan, self.policy.retention_for(table), now)
            if problems:
                run.decision = Decision.DENY
                run.finish(Outcome.DENIED, *(f"retention violation: {p}" for p in problems))
                return

        impact: ImpactReport | None = None
        if plan.kind in _POINTER_MOVES:
            impact = self._measure_pointer_move(run, table, plan)
            if run.invariants and any(not r.passed for r in run.invariants):
                run.finish(
                    Outcome.INVARIANT_FAILED,
                    *(f"invariant {r.name}: {r.message}" for r in run.invariants if not r.passed),
                )
                return

        needs_approval = decided.decision is Decision.ASK or plan.irreversible
        if (needs_approval or plan.requires_approval) and not self._approved(
            run, approver, self._request(run, decided, plan=plan, impact=impact)
        ):
            return

        run.before[table] = self.adapter.current_snapshot_id(table)
        self._intent(run)
        result = self.adapter.apply_maintenance(maintenance, plan)
        run.executed = True
        run.after[table] = result.snapshot_after
        run.published = result.snapshot_after != result.snapshot_before
        deleted = list(result.deleted_files)
        run.maintenance_result = {
            "expired_snapshot_ids": list(result.expired_snapshot_ids),
            "deleted_files": deleted[:_AUDIT_FILE_LIST_LIMIT],
            "deleted_file_count": len(deleted),
            "deleted_files_truncated": len(deleted) > _AUDIT_FILE_LIST_LIMIT,
        }
        run.finish(Outcome.MAINTENANCE_DONE)

    def _measure_pointer_move(self, run: _Run, table: str, plan: MaintenancePlan) -> ImpactReport:
        """Row and schema diff of main versus the rollback/fast-forward target, plus invariants."""
        if plan.current_snapshot_id is None or plan.target_snapshot_id is None:
            raise ExecutionError("pointer move needs both a current and a target snapshot")
        before = self.adapter.read(table, plan.current_snapshot_id)
        after = self.adapter.read(table, plan.target_snapshot_id)
        key = self.policy.invariants.for_table(table).unique_key
        impact = compute_table_impact(table, before, after, key)
        invariants = invariants_for(self.policy.invariants, table, self.extra_invariants)
        run.invariants.extend(
            evaluate(invariants, InvariantContext(table, before, after, impact), "pre_publish")
        )
        run.impact = ImpactReport((impact,))
        return run.impact

    # -- SQL writes ------------------------------------------------------------------------------

    def _unsupported_reason(self, classification: Classification) -> str | None:
        if len(classification.write_tables) != 1:
            return (
                "exactly one written table is supported per action "
                f"(got {len(classification.write_tables)})"
            )
        for statement in classification.statements:
            runnable = statement.verb in _DML_VERBS or (
                statement.verb == "SELECT" and statement.klass is C.READ
            )
            if not runnable:
                return f"{statement.verb} statements cannot be executed on a branch"
        return None

    def _run_write(
        self, run: _Run, decided: PolicyDecision, approver: Approver | None, now: datetime
    ) -> None:
        action, classification = run.action, run.classification
        if not isinstance(action, SqlAction) or classification is None:
            raise RuntimeError("write path requires a classified SQL action")
        reason = self._unsupported_reason(classification)
        if reason:
            run.finish(Outcome.UNSUPPORTED, reason)
            return
        table = classification.write_tables[0]
        if not self.catalog.has_table(table):
            run.finish(Outcome.EXECUTION_FAILED, f"table {table} does not exist")
            return

        run.before[table] = self.adapter.current_snapshot_id(table)
        self._intent(run)
        branch = self.adapter.create_branch(table)
        run.branch = branch.name
        try:
            self._rehearse_and_publish(run, decided, approver, branch, table)
        finally:
            try:
                self.adapter.drop_branch(branch)
            except Exception as err:  # cleanup must not mask the real outcome
                run.reasons.append(f"audit branch {branch.name} could not be dropped: {err}")

    def _rehearse_and_publish(
        self,
        run: _Run,
        decided: PolicyDecision,
        approver: Approver | None,
        branch: BranchHandle,
        table: str,
    ) -> None:
        action, classification = run.action, run.classification
        if not isinstance(action, SqlAction) or classification is None:
            raise RuntimeError("write path requires a classified SQL action")

        self.adapter.apply_sql(branch, action.sql, action.dialect, classification.tables)
        run.executed = True
        key = self.policy.invariants.for_table(table).unique_key
        diff = self.adapter.diff(branch, key)
        report = ImpactReport((diff.impact,))
        run.impact = report

        invariants = invariants_for(self.policy.invariants, table, self.extra_invariants)
        ctx = InvariantContext(table, diff.before, diff.after, diff.impact)
        pre = evaluate(invariants, ctx, "pre_publish")
        run.invariants.extend(pre)
        failed = [r for r in pre if not r.passed]
        if failed:
            run.finish(
                Outcome.INVARIANT_FAILED, *(f"invariant {r.name}: {r.message}" for r in failed)
            )
            return

        measured = self.policy.decide(
            classification.with_impact(report.to_impact()), run.scope, self._clock()
        )
        run.set_decision(measured)
        if measured.decision is Decision.DENY:
            run.finish(Outcome.DENIED)
            return
        if measured.decision is Decision.ASK and not self._approved(
            run, approver, self._request(run, measured, impact=report)
        ):
            return

        self._publish_and_verify(run, branch, table, diff.before, invariants, key)

    def _publish_and_verify(
        self,
        run: _Run,
        branch: BranchHandle,
        table: str,
        before: pa.Table,
        invariants: list[Invariant],
        key: Sequence[str],
    ) -> None:
        try:
            published = self.adapter.publish(branch)
        except BranchDiverged as err:
            run.decision = Decision.ASK
            run.finish(
                Outcome.REFUSED_DIVERGED,
                f"{err}; nothing was published, re-submit against the current state",
            )
            return
        if not published.changed:
            run.after[table] = published.snapshot_id
            run.finish(Outcome.NO_CHANGE, "the statement changed no rows")
            return
        run.published = True
        run.after[table] = published.snapshot_id

        problems: list[str]
        try:
            # verify the snapshot that was published, which is main unless someone wrote since
            live = self.adapter.read(table, published.snapshot_id)
            ctx = InvariantContext(
                table, before, live, compute_table_impact(table, before, live, key)
            )
            post = evaluate(invariants, ctx, "post_publish")
            run.invariants.extend(post)
            problems = [f"invariant {r.name}: {r.message}" for r in post if not r.passed]
        except Exception as err:  # unable to verify counts as failed verification
            problems = [_clip(f"post-publish verification crashed: {type(err).__name__}: {err}")]
        if not problems:
            run.finish(Outcome.PUBLISHED)
            return
        self._roll_back(run, table, published.previous_snapshot_id, published.snapshot_id, problems)

    def _roll_back(
        self, run: _Run, table: str, previous: int, published: int, problems: list[str]
    ) -> None:
        run.reasons.extend(f"post-publish check failed: {p}" for p in problems)
        try:
            self.adapter.rollback(table, previous, expected_current=published)
            restored = self.adapter.current_snapshot_id(table)
        except BranchDiverged as err:
            run.error = _clip(str(err))
            run.finish(
                Outcome.ROLLBACK_BLOCKED,
                "main has moved since the publish, so it was not rolled back automatically",
            )
            return
        except Exception as err:  # whatever fails here, main still holds unverified data
            run.error = _clip(f"{type(err).__name__}: {err}")
            run.finish(Outcome.ROLLBACK_FAILED, f"rollback failed: {run.error}")
            return
        run.after[table] = restored
        if restored != previous:
            run.finish(Outcome.ROLLBACK_FAILED, "main is not at the previous snapshot")
            return
        run.published = False
        run.rolled_back = True
        run.finish(Outcome.ROLLED_BACK, f"main restored to snapshot {previous}")

    # -- audit -----------------------------------------------------------------------------------

    def _body(self, run: _Run, phase: str) -> dict[str, Any]:
        return {
            "id": run.audit_id,
            "phase": phase,
            "action": _describe_action(run.action, self._redact),
            "task_scope": _describe_scope(run.scope),
            "classification": _describe_classification(run.classification),
            "class": run.klass,
            "decision": run.decision,
            "reasons": [_clip(r) for r in run.reasons],
            "approval": run.approval,
            "approver": run.approver_id,
            "outcome": run.outcome if phase == "completed" else None,
            "needs_attention": run.outcome in _ATTENTION_OUTCOMES,
            "executed": run.executed,
            "published": run.published,
            "rolled_back": run.rolled_back,
            "branch": run.branch,
            "impact": run.impact.to_dict() if run.impact else None,
            "invariants": [r.to_dict() for r in run.invariants],
            "maintenance_plan": _plan_for_audit(run.plan),
            "maintenance_result": run.maintenance_result,
            "rows_returned": run.data.num_rows if run.data is not None else None,
            "snapshots_before": run.before,
            "snapshots_after": run.after,
            "error": run.error,
        }

    def _intent(self, run: _Run) -> None:
        """Record what is about to be attempted; failing here stops the action before it runs."""
        self.audit.append(self._body(run, "started"))

    def _complete(self, run: _Run) -> GuardResult:
        body = self._body(run, "completed")
        audit_error: str | None = None
        try:
            self.audit.append(body)
        except AuditError as err:
            audit_error = _clip(str(err))
            _log.critical(
                "audit completion record could not be written (%s): %s",
                audit_error,
                self.audit.render(body),
            )
        return GuardResult(
            decision=run.decision,
            outcome=run.outcome,
            klass=run.klass,
            reasons=tuple(run.reasons),
            audit_id=run.audit_id,
            executed=run.executed,
            published=run.published,
            rolled_back=run.rolled_back,
            impact=run.impact,
            invariants=tuple(run.invariants),
            plan=run.plan,
            data=run.data,
            snapshots_before=dict(run.before),
            snapshots_after=dict(run.after),
            error=run.error,
            audit_error=audit_error,
        )


# -- helpers -----------------------------------------------------------------------------------


def _retention_violations(plan: MaintenancePlan, rule: RetentionRule, now: datetime) -> list[str]:
    """Why running `plan` would breach the table's minimum retention, judged on the live table.

    The policy already checked the action's arguments; this checks what the arguments actually
    select right now (how many snapshots survive, how old each doomed snapshot or file is).
    """
    problems: list[str] = []
    if plan.expired:
        if plan.remaining_snapshots < rule.min_snapshots:
            problems.append(
                f"expiry would leave {plan.remaining_snapshots} snapshot(s); "
                f"minimum is {rule.min_snapshots}"
            )
        for snap in plan.expired:
            age_days = (
                now - datetime.fromtimestamp(snap.timestamp_ms / 1000, tz=UTC)
            ).total_seconds() / 86400
            if age_days < rule.min_snapshot_age_days:
                problems.append(
                    f"snapshot {snap.snapshot_id} is {age_days:.1f} days old; minimum age to "
                    f"expire is {rule.min_snapshot_age_days:g} days"
                )
    for orphan in plan.orphan_files:
        age_days = (now - orphan.modified).total_seconds() / 86400
        if age_days < rule.min_orphan_age_days:
            problems.append(
                f"{orphan.location} is {age_days:.1f} days old; minimum age to delete is "
                f"{rule.min_orphan_age_days:g} days"
            )
    return problems


def _clip(text: str, limit: int = _ERROR_TEXT_LIMIT) -> str:
    """Engine error text can echo table values; keep only the start."""
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _approver_id(approver: Approver) -> str | None:
    """Optional identity: an `approver_id` string attribute on the approver callable."""
    identity = getattr(approver, "approver_id", None)
    return identity if isinstance(identity, str) and identity else None


def redact_sql(sql: str, dialect: str) -> str:
    """`sql` with every string/number literal replaced by `?`; unparseable SQL is not stored."""
    try:
        trees = [t for t in sqlglot.parse(sql, dialect=dialect) if t is not None]
        redacted = []
        for tree in trees:
            explain = explained(tree, dialect)
            if explain is not None:
                redacted.append(explain.render(redact_sql(explain.statement.sql, dialect)))
                continue
            for node in list(tree.find_all(*_LITERAL_NODES)):
                node.replace(exp.Placeholder())
            redacted.append(tree.sql(dialect=dialect))
        return "; ".join(redacted)
    except Exception:
        return "<unparseable SQL not stored>"


def _describe_action(action: Action, redact: bool) -> dict[str, Any]:
    if isinstance(action, SqlAction):
        return {
            "type": "sql",
            "sql": redact_sql(action.sql, action.dialect) if redact else action.sql,
            "sql_sha256": hashlib.sha256(action.sql.encode("utf-8")).hexdigest(),
            "redacted": redact,
            "dialect": action.dialect,
        }
    return {"type": type(action).__name__, **asdict(action)}


def _describe_scope(scope: TaskScope | None) -> dict[str, Any] | None:
    if scope is None:
        return None
    return {
        "allowed_tables": sorted(scope.allowed_tables),
        "allowed_namespaces": sorted(scope.allowed_namespaces),
    }


def _describe_classification(classification: Classification | None) -> dict[str, Any] | None:
    if classification is None:
        return None
    return {
        "class": classification.klass,
        "tables": list(classification.tables),
        "write_tables": list(classification.write_tables),
        "statements": [
            {"index": s.index, "verb": s.verb, "class": s.klass, "reasons": list(s.reasons)}
            for s in classification.statements
        ],
    }


def _plan_for_audit(plan: MaintenancePlan | None) -> dict[str, object] | None:
    if plan is None:
        return None
    summary = plan.to_dict()
    files = summary["orphan_files"]
    if not isinstance(files, list):
        raise TypeError("plan.to_dict()['orphan_files'] must be a list")
    summary["orphan_files"] = files[:_AUDIT_FILE_LIST_LIMIT]
    summary["orphan_files_truncated"] = len(files) > _AUDIT_FILE_LIST_LIMIT
    return summary
