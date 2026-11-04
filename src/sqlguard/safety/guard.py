"""The Guard: one entry point that classifies, decides, rehearses, verifies, and publishes.

    guard = Guard(catalog, policy, audit_path="audit.jsonl")
    result = guard.submit(SqlAction("DELETE FROM orders WHERE id = 7"), scope, approver=ask_human)

Pipeline for every submission:

    classify -> policy decision
      DENY                  -> nothing runs
      ALLOW (reads)         -> query main, return rows
      ALLOW_VIA_BRANCH      -> run on an audit branch, measure impact, check invariants, re-decide
                               with the measured impact, publish the measured snapshot only if
                               main has not moved, verify main afterwards and roll back if
                               verification fails
      ASK                   -> nothing runs without an approver; SQL writes are first rehearsed on
                               a branch so the approver sees the measured impact
      IRREVERSIBLE actions  -> need an approver, and the live retention check must pass first

With a durable `ActionStore` (`Guard(store=...)`) and no approver, an ASK is not dropped: the
write is rehearsed on a branch that is kept, the action is saved as `pending` with the digests of
the live state, the policy and the row diff that were measured, and the caller gets an `action_id`.
`Guard.resolve` later applies a decision, but only after proof of approval is verified and the
live table, the policy and the diff are checked to be what was reviewed (see `docs/architecture.md`,
"Pending actions"). `Guard.reconcile` settles actions a crash left half-way, and
`Guard.expire_pending` retires the ones nobody decided on.

Main is never written directly: a SQL write reaches main only through `publish`, which moves the
main pointer to the snapshot that was measured and verified (not to whatever the branch holds by
then). A publish whose result cannot be confirmed, or that other writers have replaced, is never
rolled back: it is reported as needing attention. An engine that cannot run a write on an
isolated branch (`Capabilities.branch_write`) is never given one: such writes are denied.
Every submission appends a completion record
to the audit log (denials and failures included), and anything with side effects first appends
a "started" record. Any unexpected error fails closed (nothing published, branch
dropped) and is reported as `Outcome.ERROR`.

This module is deterministic and contains no LLM.
"""

from __future__ import annotations

import hashlib
import logging
import os
import socket
import threading
import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Protocol

import pyarrow as pa
import sqlglot
from sqlglot import exp

from sqlguard.core.catalog import LakehouseCatalog
from sqlguard.core.findings import Decision
from sqlguard.core.parsing import explained, parse
from sqlguard.safety.actions import Action, SqlAction
from sqlguard.safety.audit import AuditError, AuditLog
from sqlguard.safety.classifier import Classification, ReversibilityClass, classify
from sqlguard.safety.executor.base import (
    BranchDiverged,
    BranchHandle,
    Capabilities,
    ExecutionError,
    ExecutorAdapter,
    MaintenancePlan,
    PublishMismatch,
    PublishStateUnknown,
    UnsupportedAction,
)
from sqlguard.safety.executor.duckdb_iceberg import AUDIT_BRANCH_PREFIX, DuckDBIcebergAdapter
from sqlguard.safety.impact import (
    DiffError,
    DiffLimits,
    ImpactReport,
    RowDiff,
    check_against_impact,
    check_size,
    compute_row_diff,
    compute_table_impact,
    row_diff,
    table_key,
)
from sqlguard.safety.invariants import (
    Invariant,
    InvariantContext,
    InvariantResult,
    evaluate,
    invariants_for,
)
from sqlguard.safety.policy import Policy, PolicyDecision, RetentionRule, TaskScope
from sqlguard.safety.store import (
    ActionRecord,
    ActionState,
    ActionStore,
    ProofReplayed,
    StoreError,
    canonical_json,
    decode_action,
    digest,
    encode_action,
    maintenance_kind,
    plan_digest,
    sha256_hex,
)

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
    # ASK and no approver was supplied. Two meanings, told apart by `GuardResult.action_id`:
    #   action_id is None   a Guard without a store: nothing was rehearsed or saved; ask again
    #                       with an approver.
    #   action_id is set    a Guard with a store: the write was rehearsed on a branch, measured,
    #                       and saved as a pending action that `Guard.resolve(action_id, ...)` can
    #                       still approve, reject or find stale. The branch is kept until then.
    AWAITING_APPROVAL = "awaiting_approval"
    REJECTED = "rejected"  # the approver declined
    READ = "read"  # query ran on main
    PUBLISHED = "published"  # change verified and live on main
    NO_CHANGE = "no_change"  # statement ran but changed no rows; main untouched
    INVARIANT_FAILED = "invariant_failed"  # pre-publish invariant failed; branch dropped
    REFUSED_DIVERGED = "refused_diverged"  # main moved while the branch was being checked
    ROLLED_BACK = "rolled_back"  # published, post-publish check failed, main restored
    ROLLBACK_FAILED = "rollback_failed"  # post-publish check failed and restoring main failed
    # left alone on purpose: the check failed but main already moved on, or the publish could
    # not be confirmed; either way undoing it could destroy another writer's commit
    ROLLBACK_BLOCKED = "rollback_blocked"
    MAINTENANCE_DONE = "maintenance_done"
    UNSUPPORTED = "unsupported"  # the adapter cannot execute this kind of action
    EXECUTION_FAILED = "execution_failed"  # the engine rejected the statement; nothing published
    ERROR = "error"  # unexpected failure; failed closed
    # outcomes of `Guard.resolve` only
    STALE = "stale"  # live table, policy or diff differ from what was reviewed; nothing applied
    EXPIRED = "expired"  # nobody decided in time; the branch was dropped


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
    action_id: str | None = None  # the pending action (see `Outcome.AWAITING_APPROVAL`)
    action_hash: str | None = None  # what an approval is bound to

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
            "action_id": self.action_id,
            "action_hash": self.action_hash,
        }


class PendingActionError(RuntimeError):
    """`Guard.resolve` was called in a way that cannot be honoured. Nothing changed."""


class UnknownAction(PendingActionError):
    """No pending action has this id."""


class ActionNotPending(PendingActionError):
    """The action was already decided, expired or settled; `state` says how it ended."""

    def __init__(self, action_id: str, state: ActionState) -> None:
        super().__init__(f"action {action_id} is {state.value}, not pending")
        self.state = state


class ProofRejected(PendingActionError):
    """The proof of approval is invalid for this action and decision, or was already used."""


@dataclass(frozen=True)
class ProofRequest:
    """What a proof of approval must vouch for: this decision, on this exact action."""

    action_id: str
    action_hash: str
    decision: Literal["approve", "reject"]
    approver_label: str


class ProofVerifier(Protocol):
    """Checks the proof that a person (not the requesting agent) made a decision.

    The Guard has no secret of its own: the approval UI or CLI produces `proof` out of band (for
    example an HMAC over the request's fields with a key the agent cannot read), and the verifier
    that was injected into the Guard checks it. Return True only for a valid proof of exactly
    this `request`. Anything else, including an exception, counts as invalid. The Guard remembers
    a hash of every accepted proof, so a verifier need not track replays.
    """

    def verify(self, request: ProofRequest, proof: str) -> bool: ...


_DECISIONS = ("approve", "reject")
DEFAULT_PENDING_TTL = timedelta(hours=24)
DEFAULT_LEASE = timedelta(minutes=5)
DEFAULT_RECONCILE_GRACE = timedelta(0)
DEFAULT_MAX_PENDING = 50
ORPHAN_BRANCH_MARGIN = timedelta(hours=1)
_ACTIVE_OWNERS: set[str] = set()  # owners of publishes running in this process right now
_ACTIVE_LOCK = threading.Lock()


class _LeaseLost(RuntimeError):
    """Another process took over the action while this one was about to publish it."""


@dataclass(frozen=True)
class Reconciled:
    """What `Guard.reconcile` concluded about one action found in `publishing`."""

    action_id: str
    state: ActionState
    reason: str


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
    action_id: str | None = None
    action_hash: str | None = None
    keep_branch: bool = False  # the branch belongs to a pending action; do not drop it
    requester: str = ""
    claimed: bool = False  # the pending action has been moved to publishing
    owner: str | None = None  # who holds the publishing action (see `Guard.resolve`)
    before_commit: Callable[[], None] | None = None  # called right before the commit
    maintenance_plan: MaintenancePlan | None = None  # verified at approval, applied right after

    def set_decision(self, decided: PolicyDecision) -> None:
        self.decision = decided.decision
        self.klass = decided.klass
        # keep the reasons of earlier stages; add only what is new
        self.reasons.extend(r for r in decided.reasons if r not in self.reasons)

    def finish(self, outcome: Outcome, *reasons: str) -> None:
        self.outcome = outcome
        self.reasons.extend(reasons)


_ATTENTION_OUTCOMES = frozenset({Outcome.ROLLBACK_FAILED, Outcome.ROLLBACK_BLOCKED})

# How an approved action ended -> its stored state. An outcome missing here (an unexpected error)
# is not known to be settled, so the action stays in `publishing` for `reconcile`.
_STATE_OF_OUTCOME: dict[Outcome, ActionState] = {
    Outcome.PUBLISHED: ActionState.PUBLISHED,
    Outcome.NO_CHANGE: ActionState.PUBLISHED,
    Outcome.MAINTENANCE_DONE: ActionState.PUBLISHED,
    Outcome.STALE: ActionState.STALE,
    Outcome.ROLLED_BACK: ActionState.FAILED,
    Outcome.EXECUTION_FAILED: ActionState.FAILED,
    Outcome.UNSUPPORTED: ActionState.FAILED,
    Outcome.ROLLBACK_BLOCKED: ActionState.NEEDS_ATTENTION,
    Outcome.ROLLBACK_FAILED: ActionState.NEEDS_ATTENTION,
}


@dataclass(frozen=True)
class _Prepared:
    """What the live checks of a write computed and `_publish_and_verify` needs."""

    diff: Any  # BranchDiff
    invariants: list[Invariant]
    key: tuple[str, ...]


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
        store: durable store for actions that need approval (an `ActionStore`, or a path to
            create one at). With it, an ASK submitted without an approver is rehearsed, saved as
            pending, and resolved later by `resolve`; without it an ASK just waits for a caller
            that has an approver. An approver passed to `submit` always gets the synchronous v1
            path, store or not.
        proof_verifier: checks the proof given to `resolve` (see `ProofVerifier`). Without one,
            `resolve` refuses every decision.
        pending_ttl: how long a pending action may wait before it expires.
        requester: label recorded for submissions that do not name one.
        diff_limits: row and byte caps for the row diff shown to an approver. A table above them
            cannot be queued for approval (a diff nobody can read is not a review). They bound
            the diff and what is listed, not the adapter, which loads both snapshots to rehearse.
        lease: how long a `resolve` that is publishing may go without renewing its hold on the
            action before `reconcile` may act on it. A heartbeat renews it while publishing.
        max_pending: queue size; with this many actions pending, further ones are refused. The
            requester label is supplied by the caller, so the cap is global, not per requester.

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
        store: ActionStore | str | Path | None = None,
        proof_verifier: ProofVerifier | None = None,
        pending_ttl: timedelta = DEFAULT_PENDING_TTL,
        requester: str = "agent",
        diff_limits: DiffLimits | None = None,
        lease: timedelta = DEFAULT_LEASE,
        max_pending: int = DEFAULT_MAX_PENDING,
    ) -> None:
        self.catalog = catalog
        self.policy = policy or Policy()
        self.audit = audit_path if isinstance(audit_path, AuditLog) else AuditLog(audit_path)
        self.adapter: ExecutorAdapter = adapter or DuckDBIcebergAdapter(catalog)
        self.extra_invariants = tuple(extra_invariants)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._redact = redact_sql_literals
        self.store: ActionStore | None = (
            store if store is None or isinstance(store, ActionStore) else ActionStore(store)
        )
        self.proof_verifier = proof_verifier
        self.pending_ttl = pending_ttl
        self.requester = requester
        self.diff_limits = diff_limits or DiffLimits()
        self.lease = lease
        self.max_pending = max_pending

    @property
    def _capabilities(self) -> Capabilities:
        # an adapter that declares nothing is assumed to be able to do nothing
        return getattr(self.adapter, "capabilities", None) or Capabilities()

    @property
    def _engine(self) -> str:
        return getattr(self.adapter, "engine", None) or type(self.adapter).__name__

    # -- public API ------------------------------------------------------------------------------

    def submit(
        self,
        action: Action,
        task_scope: TaskScope | None = None,
        approver: Approver | None = None,
        *,
        requester: str | None = None,
    ) -> GuardResult:
        """Run `action` through the pipeline and return what happened.

        `requester` labels who asked (kept with a pending action and covered by its hash).

        Never raises for a denied, failed, or rolled-back action. `AuditError` is raised only
        when the log is unwritable before anything has run (a failure to write the "started"
        record also happens before any side effect); a failure to write the final record is
        reported in `GuardResult.audit_error` instead, because the action may already be live.
        """
        self.audit.ensure_writable()
        run = _Run(action=action, scope=task_scope)
        run.requester = requester or self.requester
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
        if (
            maintenance is None
            and classification.klass is not C.READ
            and not self._capabilities.branch_write
        ):
            run.decision = Decision.DENY
            run.finish(Outcome.DENIED, f"no isolated rehearsal on {self._engine}")
            return
        queue = self.store is not None and (
            maintenance is not None or classification.klass is not C.READ
        )
        if (
            queue
            and decided.decision is Decision.ASK
            and approver is None
            and self._pending_full(run)
        ):
            return
        if decided.decision is Decision.ASK and approver is None and not queue:
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

    def _plan_checked(
        self, run: _Run, maintenance: Any, now: datetime
    ) -> tuple[str, MaintenancePlan, ImpactReport | None] | None:
        """Plan the action, then apply the retention and pointer-move checks to the live table.

        Returns None after finishing `run` (denied, or an invariant failed).
        """
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
                return None

        impact: ImpactReport | None = None
        if plan.kind in _POINTER_MOVES:
            impact = self._measure_pointer_move(run, table, plan)
            if run.invariants and any(not r.passed for r in run.invariants):
                run.finish(
                    Outcome.INVARIANT_FAILED,
                    *(f"invariant {r.name}: {r.message}" for r in run.invariants if not r.passed),
                )
                return None
        return table, plan, impact

    def _run_maintenance(
        self,
        run: _Run,
        decided: PolicyDecision,
        maintenance: Any,
        approver: Approver | None,
        now: datetime,
    ) -> None:
        checked = self._plan_checked(run, maintenance, now)
        if checked is None:
            return
        table, plan, impact = checked

        needs_approval = decided.decision is Decision.ASK or plan.irreversible
        if needs_approval or plan.requires_approval:
            if approver is None and self.store is not None:
                self._queue_maintenance(run, maintenance, table, plan, impact, now)
                return
            if not self._approved(
                run, approver, self._request(run, decided, plan=plan, impact=impact)
            ):
                return
        self._apply_maintenance(run, maintenance, table, plan)

    def _apply_maintenance(
        self, run: _Run, maintenance: Any, table: str, plan: MaintenancePlan
    ) -> None:
        run.before[table] = self.adapter.current_snapshot_id(table)
        self._intent(run)
        if run.before_commit is not None:
            run.before_commit()
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
            if not run.keep_branch:
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
        if measured.decision is Decision.ASK:
            if approver is None and self.store is not None:
                self._queue_write(run, diff, table, report)
                return
            if not self._approved(run, approver, self._request(run, measured, impact=report)):
                return

        self._publish_and_verify(run, diff.branch, table, diff.before, invariants, key)

    def _publish_and_verify(
        self,
        run: _Run,
        recorded: BranchHandle,
        table: str,
        before: pa.Table,
        invariants: list[Invariant],
        key: Sequence[str],
    ) -> None:
        try:
            if run.before_commit is not None:
                run.before_commit()
            published = self.adapter.publish(recorded, recorded.head)
        except BranchDiverged as err:
            run.decision = Decision.ASK
            run.finish(
                Outcome.REFUSED_DIVERGED,
                f"{err}; nothing was published, re-submit against the current state",
            )
            return
        except (PublishMismatch, PublishStateUnknown) as err:
            # Main may hold other writers' commits (or ours); undoing anything could destroy
            # their work, so leave main alone and ask for a person.
            if isinstance(err, PublishMismatch):
                run.after[table] = err.actual_snapshot_id
            run.error = _clip(str(err))
            run.finish(
                Outcome.ROLLBACK_BLOCKED,
                f"publish outcome needs attention: {run.error}; main was left as it is",
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
        self, run: _Run, table: str, previous: int, published: int | None, problems: list[str]
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

    # -- pending actions: queue ------------------------------------------------------------------

    @property
    def _catalog_name(self) -> str:
        return str(getattr(self.catalog.catalog, "name", "") or "")

    def _invariants_digest(self, table: str) -> str:
        return digest(
            {
                "table": asdict(self.policy.invariants.for_table(table)),
                "extra": sorted(i.name for i in self.extra_invariants),
            }
        )

    def _table_key(self, table: str):
        iceberg = self.catalog.table(table)
        return iceberg, table_key(iceberg, self.policy.invariants.for_table(table).unique_key)

    def _diff_report(self, table: str, diff: Any) -> RowDiff:
        """The row diff of a rehearsal, from the full base and head the adapter loaded.

        Nothing narrows the comparison, and it must account for exactly what the impact measured;
        otherwise (or above the size caps) it raises DiffError and the action is not offered.
        """
        iceberg, key = self._table_key(table)
        check_size(diff.before, diff.after, self.diff_limits)
        rows = row_diff(
            diff.before,
            diff.after,
            key.columns,
            table=".".join(iceberg.name()),
            base_snapshot_id=diff.branch.base_snapshot_id,
            head_snapshot_id=diff.branch.head,
            key_source=key.source,
            display_rows=self.diff_limits.display_rows,
        )
        check_against_impact(rows, diff.impact)
        return rows

    def _pending_full(self, run: _Run) -> bool:
        """True (after finishing `run`) when the queue is at its limit."""
        store = self.store
        if store is None or store.count_pending() < self.max_pending:
            return False
        run.decision = Decision.DENY
        run.finish(
            Outcome.DENIED,
            f"too many actions are waiting for approval ({self.max_pending}); decide some first",
        )
        return True

    def _seal(self, run: _Run, **fields: Any) -> ActionRecord:
        """A pending record for `run`, with the hash that an approval will be bound to."""
        store = self.store
        if store is None:
            raise RuntimeError("no action store")
        now = self._clock()
        table = fields["table"]
        record = ActionRecord(
            id=uuid.uuid4().hex,
            created_at=now,
            expires_at=now + self.pending_ttl,
            state=ActionState.PENDING,
            scope_json=canonical_json(_describe_scope(run.scope)),
            catalog_name=self._catalog_name,
            engine=self._engine,
            policy_digest=self.policy.digest,
            invariants_digest=self._invariants_digest(table),
            reasons_json=canonical_json(list(run.reasons)),
            requester=run.requester or self.requester,
            action_hash="",
            **fields,
        )
        return replace(record, action_hash=record.computed_hash())

    def _mark_pending(self, run: _Run, record: ActionRecord) -> None:
        run.action_id, run.action_hash = record.id, record.action_hash
        run.approval = "pending"
        run.finish(
            Outcome.AWAITING_APPROVAL,
            f"waiting for approval as action {record.id}; "
            f"it expires {record.expires_at.isoformat()}",
        )

    def _queue_write(self, run: _Run, diff: Any, table: str, report: ImpactReport) -> None:
        """Save a rehearsed write as a pending action and keep its branch for `resolve`."""
        store, action = self.store, run.action
        if store is None or not isinstance(action, SqlAction):
            raise RuntimeError("only SQL writes with a store can be queued here")
        if self._pending_full(run):
            return
        recorded: BranchHandle = diff.branch
        normalized = normalize_sql(action)
        redacted = redact_sql(action.sql, action.dialect)
        try:
            rows = self._diff_report(table, diff)
        except DiffError as err:  # includes DiffTooLarge: a diff nobody can read is not a review
            run.decision = Decision.DENY
            run.finish(Outcome.DENIED, f"cannot be queued for approval: {err}")
            return
        record = self._seal(
            run,
            kind="sql",
            dialect=action.dialect,
            sql_normalized=normalized,
            sql_redacted=redacted,
            sql_sha256=sha256_hex(
                redacted
            ),  # of the redacted text: a raw hash would confirm guesses
            maintenance_json=None,
            table=table,
            base_snapshot_id=recorded.base_snapshot_id,
            head_snapshot_id=recorded.head,
            branch=recorded.name,
            diff_digest=rows.digest,
            impact_json=canonical_json(
                {"impact": report.to_dict(), "row_diff": rows.to_dict(include_rows=False)}
            ),
        )
        store.insert(record)
        run.keep_branch = True  # from here the branch is the pending action's
        self._mark_pending(run, record)

    def _queue_maintenance(
        self,
        run: _Run,
        maintenance: Any,
        table: str,
        plan: MaintenancePlan,
        impact: ImpactReport | None,
        now: datetime,
    ) -> None:
        """Save a planned maintenance action as pending; approval re-plans and must match."""
        store = self.store
        if store is None:
            raise RuntimeError("no action store")
        if self._pending_full(run):
            return
        encoded = encode_action(maintenance)
        summary = {k: v for k, v in plan.to_dict().items() if k != "orphan_files"}
        record = self._seal(
            run,
            kind=maintenance_kind(maintenance),
            dialect="",
            sql_normalized=None,
            sql_redacted=f"{maintenance_kind(maintenance)}({table})",
            sql_sha256=digest(encoded),
            maintenance_json=canonical_json({"action": encoded, "plan": plan.to_dict()}),
            table=table,
            base_snapshot_id=plan.current_snapshot_id,
            head_snapshot_id=plan.target_snapshot_id,
            branch=None,
            diff_digest=plan_digest(plan),
            impact_json=canonical_json(
                {"plan": summary, "impact": impact.to_dict() if impact else None}
            ),
        )
        store.insert(record)
        self._mark_pending(run, record)

    # -- pending actions: decide -----------------------------------------------------------------

    def resolve(
        self,
        action_id: str,
        decision: Literal["approve", "reject"],
        approver_label: str,
        proof: str,
    ) -> GuardResult:
        """Approve or reject a pending action.

        `proof` is what the approval UI or CLI produced for this exact action and decision; the
        injected `ProofVerifier` must accept it. `approver_label` only names the person in the
        store and the audit log.

        Rejecting drops the branch. Approving first checks, in this order, that nothing differs
        from what was reviewed: the stored action is intact, the policy and invariants have the
        same digests, main is still on the snapshot the branch was cut from, the branch still
        ends at the rehearsed head, the statement still classifies and decides to ask or
        allow-via-branch under the current policy and the stored scope, invariants still pass,
        and the row diff recomputed from the snapshots has the digest that was reviewed. Any
        difference ends the action as `stale` (outcome `STALE`) and drops the branch. Only then
        is the proof spent, in the same transaction that moves the action to `publishing`; the
        recorded head is published by snapshot id and verified, exactly as in `submit`.

        Raises UnknownAction, ActionNotPending, ProofRejected (also for a proof that was used
        before) or ValueError for a malformed call; in each case nothing changed. Otherwise
        returns a GuardResult: PUBLISHED / MAINTENANCE_DONE, REJECTED, STALE, EXPIRED,
        ROLLED_BACK, ROLLBACK_BLOCKED or ROLLBACK_FAILED. Should something unexpected happen
        after the action reached `publishing`, the outcome is ERROR and the action stays in
        `publishing` for `reconcile` to settle from main's actual state.
        """
        store = self.store
        if store is None:
            raise PendingActionError("this Guard has no action store")
        if decision not in _DECISIONS:
            raise ValueError(f"decision must be one of {_DECISIONS}, not {decision!r}")
        label = approver_label.strip() if isinstance(approver_label, str) else ""
        if not label:
            raise ValueError("approver_label is required")
        if self.proof_verifier is None:
            raise ProofRejected("no proof verifier is configured, so no decision can be accepted")
        if not isinstance(proof, str) or not proof:
            raise ProofRejected("a proof of approval is required")
        self.audit.ensure_writable()
        record = store.get(action_id)
        if record is None:
            raise UnknownAction(f"no action {action_id}")
        if record.state is not ActionState.PENDING:
            raise ActionNotPending(action_id, record.state)

        request = ProofRequest(record.id, record.action_hash, decision, label)
        try:
            valid = self.proof_verifier.verify(request, proof) is True
        except Exception:  # noqa: BLE001 - a verifier that fails has not verified
            valid = False
        if not valid:
            self._audit_refused(record, decision, label)
            raise ProofRejected("the proof is not valid for this action and decision")

        run = self._resolution_run(record, label)
        try:
            self._resolve(run, record, decision, proof, label)
        except (AuditError, PendingActionError):
            raise
        except _LeaseLost as err:  # someone else owns the action now: touch nothing
            run.error = _clip(str(err))
            run.finish(Outcome.ERROR, run.error, "nothing was published by this call")
        except ExecutionError as err:
            run.error = _clip(str(err))
            run.finish(Outcome.EXECUTION_FAILED, run.error)
            if run.claimed:
                self._finalize(run, record, ActionState.FAILED)
                self._drop_kept_branch(run, record)
        except Exception as err:  # fail closed, and never guess what happened after the claim
            run.error = _clip(f"{type(err).__name__}: {err}")
            run.finish(Outcome.ERROR, f"unexpected error: {run.error}")
            if run.claimed:
                run.reasons.append("the action stays in publishing; Guard.reconcile will settle it")
        return self._complete(run)

    def _resolution_run(self, record: ActionRecord, label: str) -> _Run:
        maintenance = record.maintenance_data
        action: Action = (
            decode_action(maintenance["action"])
            if maintenance is not None
            else SqlAction(record.sql_normalized or "", record.dialect)
        )
        run = _Run(action=action, scope=_scope_from(record.scope_data))
        run.requester = record.requester
        run.action_id, run.action_hash = record.id, record.action_hash
        run.branch, run.approver_id = record.branch, label
        run.approval = "pending"
        return run

    def _resolve(
        self, run: _Run, record: ActionRecord, decision: str, proof: str, label: str
    ) -> None:
        store = self.store
        if store is None:
            raise RuntimeError("no action store")
        now = self._clock()
        if now >= record.expires_at:
            self._close_pending(run, record, ActionState.EXPIRED, Outcome.EXPIRED, now, label)
            return
        if decision == "reject":
            self._claim(run, record, ActionState.REJECTED, proof, label, now)
            self._drop_kept_branch(run, record)
            run.approval = "rejected"
            run.finish(Outcome.REJECTED, f"rejected by {label}")
            return

        problems, prepared = self._verify_live(run, record, now)
        if problems:
            self._close_pending(
                run, record, ActionState.STALE, Outcome.STALE, now, label, reasons=problems
            )
            return
        run.owner = _new_owner()
        with _holding(run.owner):
            self._claim(run, record, ActionState.PUBLISHING, proof, label, now)
            run.approval = "approved"
            run.claimed = True
            run.before_commit = self._renew_or_abort(record.id, run.owner)
            with self._heartbeat(record.id, run.owner):
                if prepared is None:  # maintenance
                    maintenance, plan = self._prepared_maintenance(run)
                    self._apply_maintenance(run, maintenance, record.table, plan)
                else:
                    run.before[record.table] = record.base_snapshot_id
                    self._intent(run)
                    run.executed = True
                    self._publish_and_verify(
                        run,
                        prepared.diff.branch,
                        record.table,
                        prepared.diff.before,
                        prepared.invariants,
                        prepared.key,
                    )
        if run.outcome is Outcome.REFUSED_DIVERGED:  # main moved during the publish: just stale
            run.outcome = Outcome.STALE
        state = _STATE_OF_OUTCOME.get(run.outcome)
        if state is None:  # not known to be settled: leave it to reconcile
            run.reasons.append("the action stays in publishing; Guard.reconcile will settle it")
            return
        self._finalize(run, record, state)
        self._drop_kept_branch(run, record)

    def _claim(
        self,
        run: _Run,
        record: ActionRecord,
        new: ActionState,
        proof: str,
        label: str,
        now: datetime,
    ) -> None:
        store = self.store
        if store is None:
            raise RuntimeError("no action store")
        try:
            won = store.claim(
                record.id,
                new,
                proof=proof,
                approver=label,
                now=now,
                outcome={"outcome": Outcome.REJECTED.value}
                if new is ActionState.REJECTED
                else None,
                owner=run.owner,
                lease_until=now + self.lease,
            )
        except ProofReplayed as err:
            raise ProofRejected("this proof was already used") from err
        if not won:  # someone else decided first, or time ran out in between
            current = store.get(record.id)
            raise ActionNotPending(record.id, current.state if current else ActionState.EXPIRED)

    def _close_pending(
        self,
        run: _Run,
        record: ActionRecord,
        state: ActionState,
        outcome: Outcome,
        now: datetime,
        label: str,
        reasons: Sequence[str] = (),
    ) -> None:
        """End a pending action without applying it (stale or expired) and drop its branch."""
        store = self.store
        if store is None:
            raise RuntimeError("no action store")
        summary: dict[str, Any] = {"outcome": outcome.value}
        if reasons:
            summary["reasons"] = [_clip(r) for r in reasons]
        if not store.transition(record.id, ActionState.PENDING, state, now=now, outcome=summary):
            current = store.get(record.id)
            raise ActionNotPending(record.id, current.state if current else state)
        self._drop_kept_branch(run, record)
        run.approval = state.value
        run.decision = Decision.ASK
        run.finish(outcome, *(reasons or (f"the action is {state.value}",)))

    def _finalize(self, run: _Run, record: ActionRecord, state: ActionState) -> None:
        store = self.store
        if store is None:
            raise RuntimeError("no action store")
        summary = {
            "outcome": run.outcome.value,
            "published": run.published,
            "rolled_back": run.rolled_back,
            "audit_id": run.audit_id,
        }
        if not store.transition(
            record.id,
            ActionState.PUBLISHING,
            state,
            now=self._clock(),
            outcome=summary,
            owner=run.owner,
        ):
            run.reasons.append("another process already settled this action")

    def _drop_kept_branch(self, run: _Run, record: ActionRecord) -> bool:
        """Drop a finished action's audit branch; True once it is gone (or must stay untouched).

        Only branches this Guard names are ever dropped. A failed drop leaves the row marked
        `drop_pending`, and `expire_pending` / `reconcile` try again.
        """
        store = self.store
        if record.branch is None or record.base_snapshot_id is None:
            return True
        if not record.branch.startswith(AUDIT_BRANCH_PREFIX):
            run.reasons.append(f"branch {record.branch} is not an audit branch; left alone")
            if store is not None:
                store.mark_dropped(record.id)
            return True
        try:
            self.adapter.drop_branch(
                BranchHandle(
                    record.table, record.branch, record.base_snapshot_id, record.head_snapshot_id
                )
            )
        except Exception as err:  # cleanup must not mask the real outcome
            _log.warning("could not drop branch %s: %s", record.branch, err, exc_info=True)
            run.reasons.append(
                f"audit branch {record.branch} could not be dropped (it will be retried)"
            )
            return False
        if store is not None:
            store.mark_dropped(record.id)
        return True

    @contextmanager
    def _heartbeat(self, action_id: str, owner: str) -> Iterator[None]:
        """Keep renewing the lease on a publishing action while the caller works."""
        store = self.store
        stop = threading.Event()

        def beat() -> None:
            while not stop.wait(max(self.lease.total_seconds() / 3, 0.05)):
                try:
                    if store is None or not store.renew_lease(
                        action_id, owner, self._clock() + self.lease
                    ):
                        return
                except Exception:  # noqa: BLE001 - the next beat or the pre-commit check decides
                    _log.warning("could not renew the lease on action %s", action_id)

        thread = threading.Thread(target=beat, name=f"sqlguard-lease-{action_id[:8]}", daemon=True)
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join(timeout=5)

    def _renew_or_abort(self, action_id: str, owner: str) -> Callable[[], None]:
        """A check for just before the commit: still the owner, with a fresh lease, or abort."""

        def check() -> None:
            store = self.store
            if store is None or not store.renew_lease(action_id, owner, self._clock() + self.lease):
                raise _LeaseLost(f"action {action_id} was taken over by another process")

        return check

    def _audit_refused(self, record: ActionRecord, decision: str, label: str) -> None:
        try:
            self.audit.append(
                {
                    "phase": "completed",
                    "outcome": "proof_rejected",
                    "action_id": record.id,
                    "decision": decision,
                    "approver": label,
                }
            )
        except AuditError:  # the refusal stands whether or not it could be logged
            _log.error("proof rejection for action %s could not be logged", record.id)

    # -- pending actions: live checks ------------------------------------------------------------

    def _verify_live(
        self, run: _Run, record: ActionRecord, now: datetime
    ) -> tuple[list[str], _Prepared | None]:
        """Why the action can no longer be applied as reviewed (empty when it still can)."""
        problems: list[str] = []
        if record.computed_hash() != record.action_hash:
            problems.append("the stored action no longer matches its hash")
        if record.policy_digest != self.policy.digest:
            problems.append("the policy changed since the action was reviewed")
        if record.invariants_digest != self._invariants_digest(record.table):
            problems.append("the invariants changed since the action was reviewed")
        if record.engine != self._engine or record.catalog_name != self._catalog_name:
            problems.append(
                f"the action was queued on {record.catalog_name}/{record.engine}, "
                f"this Guard runs on {self._catalog_name}/{self._engine}"
            )
        if problems:
            return problems, None
        try:
            if record.maintenance_json is not None:
                return self._verify_maintenance(run, record, now), None
            return self._verify_write(run, record, now)
        except DiffError as err:  # its message is written to be shown
            return [f"the live state could not be checked: {err}"], None
        except (ExecutionError, StoreError, KeyError, ValueError) as err:
            _log.warning("live check of action %s failed", record.id, exc_info=True)
            return [f"the live state could not be checked ({type(err).__name__})"], None

    def _verify_write(
        self, run: _Run, record: ActionRecord, now: datetime
    ) -> tuple[list[str], _Prepared | None]:
        action = run.action
        if (
            not isinstance(action, SqlAction)
            or not action.sql
            or record.branch is None
            or record.base_snapshot_id is None
            or record.head_snapshot_id is None
        ):
            return ["the stored action is incomplete"], None
        classification = classify(action, self.catalog)
        run.classification = classification
        if classification.write_tables != (record.table,):
            return [f"the statement no longer writes exactly {record.table}"], None
        reason = self._unsupported_reason(classification)
        if reason:
            return [reason], None
        if not record.branch.startswith(AUDIT_BRANCH_PREFIX):
            return [f"{record.branch} is not an audit branch"], None
        main = self.adapter.current_snapshot_id(record.table)
        if main != record.base_snapshot_id:
            return [
                f"main moved from snapshot {record.base_snapshot_id} to {main} since the review"
            ], None
        key = self.policy.invariants.for_table(record.table).unique_key
        handle = BranchHandle(
            record.table, record.branch, record.base_snapshot_id, record.head_snapshot_id
        )
        diff = self.adapter.diff(handle, key)
        if diff.branch.head != record.head_snapshot_id:
            return [
                f"the branch moved from snapshot {record.head_snapshot_id} to "
                f"{diff.branch.head} after the review"
            ], None
        measured = self.policy.decide(
            classification.with_impact(ImpactReport((diff.impact,)).to_impact()), run.scope, now
        )
        run.set_decision(measured)
        if measured.decision not in (Decision.ASK, Decision.ALLOW_VIA_BRANCH):
            return [
                f"the current policy now says {measured.decision.value}: "
                + "; ".join(measured.reasons)
            ], None
        invariants = invariants_for(self.policy.invariants, record.table, self.extra_invariants)
        pre = evaluate(
            invariants,
            InvariantContext(record.table, diff.before, diff.after, diff.impact),
            "pre_publish",
        )
        run.invariants.extend(pre)
        failed = [f"invariant {r.name}: {r.message}" for r in pre if not r.passed]
        if failed:
            return failed, None
        rows = self._diff_report(record.table, diff)
        if rows.digest != record.diff_digest:
            return ["the row diff differs from the one that was reviewed"], None
        return [], _Prepared(diff, invariants, tuple(key))

    def _verify_maintenance(self, run: _Run, record: ActionRecord, now: datetime) -> list[str]:
        classification = classify(run.action, self.catalog)
        run.classification = classification
        decided = self.policy.decide(classification, run.scope, now)
        run.set_decision(decided)
        if decided.decision is Decision.DENY:
            return ["the current policy now denies this action: " + "; ".join(decided.reasons)]
        maintenance = classification.statements[0].maintenance
        if maintenance is None or classification.write_tables != (record.table,):
            return ["the stored maintenance action no longer classifies as the same action"]
        checked = self._plan_checked(run, maintenance, now)
        if checked is None:
            return [f"the plan is no longer allowed ({run.outcome.value})"]
        _, plan, _ = checked
        if plan_digest(plan) != record.diff_digest:
            return ["the plan differs from the one that was reviewed"]
        run.maintenance_plan = plan
        return []

    def _prepared_maintenance(self, run: _Run) -> tuple[Any, MaintenancePlan]:
        plan, classification = run.maintenance_plan, run.classification
        if plan is None or classification is None:
            raise RuntimeError("maintenance was not verified")
        return classification.statements[0].maintenance, plan

    # -- pending actions: housekeeping -----------------------------------------------------------

    def _mine(self, record: ActionRecord) -> bool:
        """Whether this Guard's catalog and engine are the ones the action was queued for."""
        return record.catalog_name == self._catalog_name and record.engine == self._engine

    def expire_pending(self, now: datetime | None = None) -> list[str]:
        """Expire this Guard's pending actions past their time and drop their branches.

        Returns the expired ids. Also retries branch drops that failed earlier, sweeps orphaned
        audit branches (`sweep_orphan_branches`) and empties the store's WAL if a reader had
        blocked that before. Actions queued for another catalog or engine are left alone.
        """
        store = self.store
        if store is None:
            raise PendingActionError("this Guard has no action store")
        moment = now or self._clock()
        expired = store.expire_due(moment, catalog_name=self._catalog_name, engine=self._engine)
        for record in expired:
            scratch = _Run(action=SqlAction(""), scope=None)
            self._drop_kept_branch(scratch, record)
            self._log_settled(
                {
                    "outcome": Outcome.EXPIRED.value,
                    "action_id": record.id,
                    "branch": record.branch,
                    "reasons": scratch.reasons,
                }
            )
        self._housekeeping(moment)
        return [r.id for r in expired]

    def _housekeeping(self, now: datetime) -> None:
        store = self.store
        if store is None:
            return
        try:
            for record in store.list_drop_pending(
                catalog_name=self._catalog_name, engine=self._engine
            ):
                self._drop_kept_branch(_Run(action=SqlAction(""), scope=None), record)
            self.sweep_orphan_branches(now)
        except Exception:  # noqa: BLE001 - housekeeping must not fail the caller's real work
            _log.warning("branch housekeeping failed", exc_info=True)
        store.scrub()

    def sweep_orphan_branches(self, now: datetime | None = None) -> list[str]:
        """Drop `sqlguard-` branches that no unfinished action owns and that hold stale data.

        An audit branch holds the rows a rehearsal produced, and pins their data files against
        expiry and orphan cleanup. One is swept only when its head snapshot is not on main's
        history (a branch still at its base holds nothing and may belong to a submission that
        is running right now) and is older than the pending lifetime plus an hour, so no live
        submission or pending action can own it. Returns `table:branch` for each one dropped.
        """
        store = self.store
        if store is None:
            raise PendingActionError("this Guard has no action store")
        moment = now or self._clock()
        cutoff_ms = int((moment - self.pending_ttl - ORPHAN_BRANCH_MARGIN).timestamp() * 1000)
        owned = store.live_branches()
        swept: list[str] = []
        catalog = self.catalog.catalog
        for namespace in catalog.list_namespaces():
            for identifier in catalog.list_tables(namespace):
                name = ".".join(identifier)
                try:
                    iceberg = catalog.load_table(identifier)
                    on_main = set(self._ancestry(iceberg, None))
                    for ref_name, ref in iceberg.refs().items():
                        if (
                            ref_name == "main"
                            or not ref_name.startswith(AUDIT_BRANCH_PREFIX)
                            or ref_name in owned
                            or ref.snapshot_ref_type.value != "branch"
                            or ref.snapshot_id in on_main
                        ):
                            continue
                        snapshot = iceberg.snapshot_by_id(ref.snapshot_id)
                        if snapshot is None or snapshot.timestamp_ms >= cutoff_ms:
                            continue
                        self.adapter.drop_branch(
                            BranchHandle(name, ref_name, ref.snapshot_id, ref.snapshot_id)
                        )
                        swept.append(f"{name}:{ref_name}")
                except Exception:  # noqa: BLE001 - one unreadable table must not stop the sweep
                    _log.warning("could not sweep branches of %s", name, exc_info=True)
        for entry in swept:
            self._log_settled({"outcome": "branch_swept", "branch": entry})
        return swept

    def _log_settled(self, record: dict[str, Any]) -> None:
        try:
            self.audit.append({"phase": "completed", **record})
        except AuditError:
            _log.error("could not log %s", record.get("outcome"))

    def reconcile(self, grace: timedelta = DEFAULT_RECONCILE_GRACE) -> list[Reconciled]:
        """Settle actions found in `publishing` whose publisher is gone.

        Safe to call from several processes and while resolves are running. An action in
        `publishing` has an owner (host, boot id, PID namespace, pid, token) and a lease that the
        owner renews while it works. Reconcile leaves a row alone while its lease is fresh (plus
        `grace`), while the owner is a live process in this PID namespace or is working in this
        process, and when it was queued for a different catalog or engine. Otherwise it takes
        ownership by compare-and-set (two reconcilers cannot both settle a row) and decides
        from main:

        - main on the approved head: post-publish checks run; `published`, or rolled back to
          the base (`failed`) only if main is still exactly on the head;
        - main on a later snapshot with the head in its history: verified without any rollback
          (`published`, or `needs_attention`); for a backward pointer move this holds only if
          the base is no longer in main's history;
        - main still on the base: `failed` only if the owner is proven dead (same host, boot id and
          PID namespace, all known); otherwise its commit might still be in flight, so
          `needs_attention`;
        - anything else, and expiry or orphan cleanup: `needs_attention`.

        A `needs_attention` row keeps its branch. Returns what it settled. A crashed publish
        keeps its lease until it runs out, so a reconcile right after the crash skips it; run it
        again once the lease (default 5 minutes) has passed.
        """
        store = self.store
        if store is None:
            raise PendingActionError("this Guard has no action store")
        self.audit.ensure_writable()
        settled: list[Reconciled] = []
        for record in store.list_actions([ActionState.PUBLISHING], limit=10_000):
            if not self._mine(record):
                continue
            now = self._clock()
            if record.lease_until is not None and record.lease_until + grace > now:
                continue
            status = _owner_status(record.owner)
            if status == "alive":
                continue
            me = _new_owner()
            with _holding(me):
                if not store.take_over(
                    record.id,
                    expected_owner=record.owner,
                    new_owner=me,
                    now=now - grace,
                    lease_until=now + self.lease,
                ):
                    continue
                try:
                    state, reason, keep = self._judge_publishing(record, status)
                except Exception as err:  # noqa: BLE001 - unreadable table: a person has to look
                    state, keep = ActionState.NEEDS_ATTENTION, True
                    _log.warning("could not judge action %s", record.id, exc_info=True)
                    reason = f"could not be checked ({type(err).__name__}); check the table"
                summary = {"outcome": "reconciled", "reason": reason}
                if not store.transition(
                    record.id,
                    ActionState.PUBLISHING,
                    state,
                    now=self._clock(),
                    outcome=summary,
                    owner=me,
                    keep_branch=keep,
                ):
                    continue  # lost the row meanwhile
            if not keep:
                self._drop_kept_branch(_Run(action=SqlAction(""), scope=None), record)
            self._log_settled(
                {
                    "outcome": "reconciled",
                    "action_id": record.id,
                    "state": state.value,
                    "reason": reason,
                    "needs_attention": state is ActionState.NEEDS_ATTENTION,
                }
            )
            settled.append(Reconciled(record.id, state, reason))
        self._housekeeping(self._clock())
        return settled

    def _judge_publishing(
        self, record: ActionRecord, owner_status: str
    ) -> tuple[ActionState, str, bool]:
        """(state, reason, keep the branch) for a publishing row nobody owns any more."""
        base, head, table = record.base_snapshot_id, record.head_snapshot_id, record.table
        attention = ActionState.NEEDS_ATTENTION
        if record.maintenance_json is not None and record.kind not in _POINTER_MOVES:
            return (
                attention,
                f"{record.kind} was interrupted; check the table and the audit log",
                True,
            )
        main = self.adapter.current_snapshot_id(table)
        if main == base:
            if owner_status == "dead":
                return (
                    ActionState.FAILED,
                    "main is still on the snapshot it had; nothing applied",
                    False,
                )
            return (
                attention,
                "main is still on the snapshot it had, but the publisher is on another host "
                "and its commit may still land",
                True,
            )
        pointer = record.maintenance_json is not None
        if main == head:
            if pointer:
                return ActionState.PUBLISHED, "main is on the target snapshot", False
            state, reason = self._verify_landed(record, rollback=True)
            return state, reason, state is attention
        if head is not None and head in (
            history := self._ancestry(self.catalog.table(table), None)
        ):
            if pointer:
                forward = base in self._ancestry(self.catalog.table(table), head)
                if forward or base not in history:
                    return ActionState.PUBLISHED, "main has moved on past the target", False
                return (
                    attention,
                    f"main is on {main}, which has the target in its history but also the "
                    f"snapshot the move started from, so the move did not happen",
                    True,
                )
            state, reason = self._verify_landed(record, rollback=False)
            return state, reason, state is attention
        return attention, f"main is on snapshot {main}, which is neither {base} nor {head}", True

    @staticmethod
    def _ancestry(iceberg: Any, snapshot_id: int | None) -> list[int]:
        """Snapshot ids from `snapshot_id` (default: main's head) back through the parents."""
        chain: list[int] = []
        current = (
            iceberg.current_snapshot()
            if snapshot_id is None
            else iceberg.snapshot_by_id(snapshot_id)
        )
        while current is not None and current.snapshot_id not in chain:
            chain.append(current.snapshot_id)
            parent = current.parent_snapshot_id
            current = iceberg.snapshot_by_id(parent) if parent is not None else None
        return chain

    def _verify_landed(self, record: ActionRecord, *, rollback: bool) -> tuple[ActionState, str]:
        """Post-publish checks for a write found on main; undo it only if main is exactly on it."""
        table, base, head = record.table, record.base_snapshot_id, record.head_snapshot_id
        if base is None or head is None:
            return ActionState.NEEDS_ATTENTION, "the stored action is incomplete"
        key = self.policy.invariants.for_table(table).unique_key
        invariants = invariants_for(self.policy.invariants, table, self.extra_invariants)
        try:
            before = self.adapter.read(table, base)
            live = self.adapter.read(table, head)
            ctx = InvariantContext(
                table, before, live, compute_table_impact(table, before, live, key)
            )
            problems = [r for r in evaluate(invariants, ctx, "post_publish") if not r.passed]
        except Exception:  # noqa: BLE001 - unable to verify counts as failed verification
            _log.warning("post-publish check of %s failed", table, exc_info=True)
            problems = [
                InvariantResult("verification", table, "post_publish", False, "could not be run")
            ]
        if not problems:
            return ActionState.PUBLISHED, "the publish had landed and its checks pass"
        text = "; ".join(f"{r.name}: {r.message}" for r in problems)
        if not rollback:
            return (
                ActionState.NEEDS_ATTENTION,
                f"the publish landed, other commits followed, and its checks fail ({text})",
            )
        try:
            self.adapter.rollback(table, base, expected_current=head)
        except Exception:  # noqa: BLE001
            _log.warning("rollback of %s failed", table, exc_info=True)
            return (
                ActionState.NEEDS_ATTENTION,
                f"checks fail ({text}) and the rollback failed",
            )
        return ActionState.FAILED, f"the publish landed, its checks failed ({text}); rolled back"

    def diff_for(self, action_id: str) -> RowDiff:
        """The row diff of a SQL action, recomputed from its snapshots right now.

        Compare `RowDiff.digest` with `ActionRecord.diff_digest`: if they differ, what is shown
        is not what was reviewed, and `resolve` will find the action stale.
        """
        store = self.store
        if store is None:
            raise PendingActionError("this Guard has no action store")
        record = store.get(action_id)
        if record is None:
            raise UnknownAction(f"no action {action_id}")
        if (
            record.base_snapshot_id is None
            or record.head_snapshot_id is None
            or record.maintenance_json
        ):
            raise PendingActionError("only a SQL write has a row diff")
        iceberg, key = self._table_key(record.table)
        return compute_row_diff(
            iceberg,
            record.base_snapshot_id,
            record.head_snapshot_id,
            key=key,
            limits=self.diff_limits,
        )

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
            **({"action_id": run.action_id} if run.action_id else {}),
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
            action_id=run.action_id,
            action_hash=run.action_hash,
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


def _boot_id() -> str | None:
    """Linux boot id; None where the platform has no such thing (it cannot be told apart)."""
    try:
        with open("/proc/sys/kernel/random/boot_id", encoding="ascii") as handle:
            return handle.read().strip() or None
    except OSError:
        return None


def _pid_namespace() -> str | None:
    """Identity of this process's PID namespace (Linux); None where it cannot be known."""
    try:
        return os.readlink("/proc/self/ns/pid")
    except OSError:
        return None


def _new_owner() -> str:
    """An identity for one publish: `host|boot|pid-namespace|pid|token`.

    Host, boot id and PID namespace together say where a pid can be looked up; `-` marks a part
    the platform cannot tell (macOS), which makes the owner unprovable on any other process.
    The token is unique to the attempt.
    """
    return "|".join(
        (
            socket.gethostname(),
            _boot_id() or "-",
            _pid_namespace() or "-",
            str(os.getpid()),
            uuid.uuid4().hex[:12],
        )
    )


@contextmanager
def _holding(owner: str) -> Iterator[None]:
    with _ACTIVE_LOCK:
        _ACTIVE_OWNERS.add(owner)
    try:
        yield
    finally:  # also when the work is cut short by a BaseException
        with _ACTIVE_LOCK:
            _ACTIVE_OWNERS.discard(owner)


def _owner_status(owner: str | None) -> str:
    """`alive` (leave it alone), `dead` (proven gone) or `unknown` (rely on the lease).

    `dead` needs the owner's host, boot id and PID namespace to equal this process's, all of
    them known, so that "no process with this pid" really means this owner is gone. A peer in
    another container (same hostname, own PID namespace), after a reboot, on another machine, or
    on a platform that cannot tell is `unknown`: only the lease expiring, and then
    `needs_attention` where evidence is missing, applies.
    """
    if not owner:
        return "unknown"
    with _ACTIVE_LOCK:
        if owner in _ACTIVE_OWNERS:
            return "alive"
    parts = owner.split("|")
    if len(parts) != 5 or not parts[3].isdigit():
        return "unknown"
    here = (socket.gethostname(), _boot_id() or "-", _pid_namespace() or "-")
    if tuple(parts[:3]) != here or "-" in here:
        return "unknown"
    pid = int(parts[3])
    if pid == os.getpid():
        return "dead"  # this process, and it is not working on it
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "dead"
    except PermissionError:
        return "alive"
    return "alive"


def normalize_sql(action: SqlAction) -> str:
    """The statements of `action` as sqlglot writes them, without comments: one text per meaning."""
    return ";\n".join(
        statement.ast.sql(dialect=action.dialect, comments=False)
        for statement in parse(action.sql, action.dialect)
    )


def _scope_from(data: dict[str, Any] | None) -> TaskScope | None:
    if data is None:
        return None
    return TaskScope(
        frozenset(data.get("allowed_tables", ())), frozenset(data.get("allowed_namespaces", ()))
    )


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
