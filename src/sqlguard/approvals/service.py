"""What the web UI and the CLI share: read the queue, check an action, decide it, housekeeping.

Nothing here knows about HTTP or a terminal. Everything shown about a pending action is derived
from data the action's hash binds, and the row diff is computed again from the snapshots when it
is asked for, so a store that was edited by hand shows up as a warning instead of being believed.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from sqlguard.approvals.proof import HmacProofs
from sqlguard.safety.actions import SqlAction
from sqlguard.safety.audit import AuditError
from sqlguard.safety.classifier import classify
from sqlguard.safety.guard import Guard, GuardResult, ProofRequest, redact_sql
from sqlguard.safety.impact import RowDiff
from sqlguard.safety.store import ALTER_KIND, ActionRecord, ActionState, schema_plan_digest

_log = logging.getLogger("sqlguard.approvals")

PAGE_SIZE = 25
MAX_NOTE_CHARS = 1000
MAX_LABEL_CHARS = 64
RECENT_FINISHED = 50
DIFF_CACHE_SECONDS = 30.0
DIFF_CACHE_ENTRIES = 16
AUDIT_TAIL = 200
_ID_PREFIX_MIN = 4

NEEDS_ATTENTION_HELP = (
    "The guard could not settle this action by itself: a publish or maintenance run ended in a "
    "state it cannot prove is complete or undone. Nothing is rolled back automatically, and its "
    "audit branch is kept as evidence. Compare the table's current snapshot with the base "
    "snapshot below and read the audit log before doing anything else."
)
CANNOT_PROVE_GONE_HELP = (
    "Why a crashed publish is needs_attention and not failed: the guard calls a publisher dead "
    "only when it can look its process up, which needs the host, the boot id and the PID "
    "namespace to be known and equal to its own. macOS has no boot id or PID namespace to "
    "compare, and a process in another container, another machine or an earlier boot cannot be "
    "looked up either. So the commit of a publisher that stopped could in principle still land, "
    "and the action is marked needs_attention even though main usually never moved. If the "
    "table's current snapshot is still the base snapshot shown here, nothing was applied."
)
_CANNOT_PROVE_GONE = "cannot be proven gone"

Decision = Literal["approve", "reject"]


class ReviewError(ValueError):
    """A request that cannot be honoured; the message is written to be shown to the person."""


@dataclass(frozen=True)
class Counts:
    added: int
    deleted: int
    modified: int | None  # None: unkeyed, a modified row is a deleted plus an added row

    @property
    def text(self) -> str:
        modified = "" if self.modified is None else f" ~{self.modified}"
        return f"+{self.added} -{self.deleted}{modified}"


@dataclass(frozen=True)
class QueueRow:
    record: ActionRecord
    counts: Counts | None
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class HousekeepingStatus:
    last_run: datetime | None = None
    last_error: str | None = None
    settled: int = 0
    expired: int = 0


@dataclass(frozen=True)
class QueueView:
    attention: tuple[QueueRow, ...]
    pending: tuple[QueueRow, ...]
    publishing: tuple[QueueRow, ...]
    finished: tuple[QueueRow, ...]
    housekeeping: HousekeepingStatus
    approver: str
    attention_help: str = NEEDS_ATTENTION_HELP


@dataclass(frozen=True)
class DetailView:
    record: ActionRecord
    sql: str
    problems: tuple[str, ...]  # why approving is blocked (empty: it may be approved)
    diff: RowDiff | None  # recomputed from the snapshots for this view
    reviewed: Counts | None  # what was stored when the action was queued
    rows: tuple[Any, ...]  # the RowChange rows of the requested page
    page: int
    pages: int
    can_decide: bool
    invariants: dict[str, Any]
    help: tuple[str, ...] = ()
    approve_proof: str | None = None
    reject_proof: str | None = None

    @property
    def counts(self) -> Counts | None:
        d = self.diff
        return None if d is None else Counts(d.rows_added, d.rows_deleted, d.rows_modified)

    @property
    def can_approve(self) -> bool:
        return self.can_decide and not self.problems


@dataclass(frozen=True)
class Decided:
    result: GuardResult
    note_recorded: bool = True


@dataclass
class _Cached:
    at: float
    diff: RowDiff


def approver_label(text: str) -> str:
    """A person's label for the audit trail (not authentication)."""
    label = " ".join(text.split())
    if not label or len(label) > MAX_LABEL_CHARS or not label.isprintable():
        raise ValueError(f"the approver label must be 1-{MAX_LABEL_CHARS} printable characters")
    return label


def counts_of(record: ActionRecord) -> Counts | None:
    try:
        diff = record.impact_data.get("row_diff")
        if not isinstance(diff, dict):
            return None
        return Counts(
            int(diff["rows_added"]), int(diff["rows_deleted"]), _optional_int(diff["rows_modified"])
        )
    except (KeyError, TypeError, ValueError):
        return None


def _optional_int(value: Any) -> int | None:
    return None if value is None else int(value)


class ReviewService:
    """Reading and deciding pending actions through one `Guard`.

    Calls into the Guard for a page or a decision are serialized with one lock: this is a
    single-person tool. Reading the queue only touches the store and is not blocked by a slow
    publish. Housekeeping has its own lock and does not hold that one, so a full-catalog sweep
    never makes a page or a decision wait; `Guard.reconcile` and `resolve` are safe to run
    together (every state change is a compare-and-set in the store, and a publish in progress
    in this process is recognised as alive).
    """

    def __init__(
        self,
        guard: Guard,
        proofs: HmacProofs,
        approver: str,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if guard.store is None:
            raise ValueError("the Guard needs an action store")
        if guard.proof_verifier is not proofs:
            raise ValueError("the Guard must verify proofs with the same HmacProofs")
        self.guard = guard
        self.store = guard.store
        self.proofs = proofs
        self.approver = approver_label(approver)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = threading.RLock()
        self._housekeeping_lock = threading.Lock()  # one pass at a time; pages never wait for it
        self._status = HousekeepingStatus()
        self._diffs: dict[tuple[Any, ...], _Cached] = {}

    # -- finding actions ---------------------------------------------------------------------

    def find(self, action_id: str) -> ActionRecord:
        """The action with this id, or the only one whose id starts with it (at least 4 chars)."""
        record = self.store.get(action_id)
        if record is not None:
            return record
        if len(action_id) < _ID_PREFIX_MIN or not re.fullmatch(r"[0-9a-f]+", action_id):
            raise ReviewError(f"no action {action_id}")
        matches = [r for r in self.store.list_actions(limit=10_000) if r.id.startswith(action_id)]
        if not matches:
            raise ReviewError(f"no action {action_id}")
        if len(matches) > 1:
            raise ReviewError(f"{action_id} matches {len(matches)} actions; type more of the id")
        return matches[0]

    # -- queue -------------------------------------------------------------------------------

    def queue(self) -> QueueView:
        now = self._clock()
        attention: list[QueueRow] = []
        pending: list[QueueRow] = []
        publishing: list[QueueRow] = []
        finished: list[QueueRow] = []
        for record in self.store.list_actions(limit=500):
            row = QueueRow(record, counts_of(record), self._queue_warnings(record, now))
            if record.state is ActionState.NEEDS_ATTENTION:
                attention.append(row)
            elif record.state is ActionState.PENDING:
                pending.append(row)
            elif record.state is ActionState.PUBLISHING:
                publishing.append(row)
            elif len(finished) < RECENT_FINISHED:
                finished.append(row)
        return QueueView(
            tuple(attention),
            tuple(pending),
            tuple(publishing),
            tuple(finished),
            self._status,
            self.approver,
        )

    @staticmethod
    def _queue_warnings(record: ActionRecord, now: datetime) -> tuple[str, ...]:
        if record.state is ActionState.PENDING and record.expires_at <= now:
            return ("past its deadline; the next housekeeping run expires it",)
        if (
            record.state is ActionState.PUBLISHING
            and record.lease_until is not None
            and record.lease_until <= now
        ):
            minutes = int((now - record.lease_until).total_seconds() // 60)
            return (
                f"stuck: its publisher stopped renewing the lease {minutes} min ago; "
                "housekeeping settles it from the table's state",
            )
        return ()

    # -- one action --------------------------------------------------------------------------

    def view(self, action_id: str, *, page: int = 1, mint_proofs: bool = False) -> DetailView:
        """Everything about one action, checked against live data.

        For a pending SQL write the row diff is recomputed from the base and head snapshots and
        compared with the digest that was reviewed; any difference, an altered record, a changed
        policy, a table that moved or a deadline that passed blocks approval (`problems`).
        `mint_proofs` adds the proofs the page's buttons carry.
        """
        record = self.find(action_id)
        now = self._clock()
        problems: list[str] = []
        if record.computed_hash() != record.action_hash:
            problems.append("the stored action no longer matches its hash; it was altered")
        sql = record.sql_redacted
        if record.sql_normalized is not None:
            # show what the bound SQL says, not the stored display copy
            sql = redact_sql(record.sql_normalized, record.dialect)
            if sql != record.sql_redacted:
                problems.append("the stored SQL display differs from the SQL that will run")
        decidable = record.state is ActionState.PENDING
        if decidable and record.expires_at <= now:
            decidable = False
            problems.append("the deadline passed; the next housekeeping run expires it")
        diff: RowDiff | None = None
        if decidable:
            problems.extend(self._live_problems(record))
            if record.kind == "sql":
                diff, why = self._diff(record)
                if why:
                    problems.append(why)
        pages = 1 if diff is None else max(1, -(-len(diff.rows) // PAGE_SIZE))
        page = min(max(page, 1), pages)
        rows = () if diff is None else diff.rows[(page - 1) * PAGE_SIZE : page * PAGE_SIZE]
        approve = reject = None
        if mint_proofs and decidable:
            if not problems:
                approve = self.proofs.mint(self._request(record, "approve"))
            reject = self.proofs.mint(self._request(record, "reject"))
        return DetailView(
            record=record,
            sql=sql,
            problems=tuple(problems),
            diff=diff,
            reviewed=counts_of(record),
            rows=rows,
            page=page,
            pages=pages,
            can_decide=decidable,
            invariants=self._invariants(record.table),
            help=self._help(record),
            approve_proof=approve,
            reject_proof=reject,
        )

    def _request(self, record: ActionRecord, decision: Decision) -> ProofRequest:
        return ProofRequest(record.id, record.action_hash, decision, self.approver)

    def _live_problems(self, record: ActionRecord) -> list[str]:
        problems: list[str] = []
        if record.policy_digest != self.guard.policy.digest:
            problems.append("the policy this server runs differs from the one the action met")
        try:
            with self._lock:
                main = self.guard.adapter.current_snapshot_id(record.table)
                refs = self.guard.catalog.refs(record.table) if record.branch else {}
        except Exception:  # noqa: BLE001 - an unreadable table must not hide the page
            _log.warning("could not read %s for action %s", record.table, record.id, exc_info=True)
            return [*problems, f"could not read the current state of {record.table}"]
        if record.branch and record.branch not in refs:
            problems.append(f"the audit branch {record.branch} no longer exists")
        elif record.branch and refs[record.branch] != record.head_snapshot_id:
            problems.append(
                f"the audit branch moved from snapshot {record.head_snapshot_id} to "
                f"{refs[record.branch]} after the review"
            )
        if main != record.base_snapshot_id:
            problems.append(
                f"{record.table} moved from snapshot {record.base_snapshot_id} to {main} after "
                "the review; approving would end as stale. Reject it, or let it expire."
            )
        if record.kind == ALTER_KIND:
            problems.extend(self._schema_problems(record))
        elif record.kind == "sql" and self._schema_moved(record):
            problems.append(
                "the table's columns changed after the review; approving would end as stale. "
                "Reject it, or let it expire."
            )
        return problems

    def _schema_moved(self, record: ActionRecord) -> bool:
        try:
            with self._lock:
                return self.guard.schema_moved(record)
        except Exception:  # noqa: BLE001 - an unreadable table is reported by the checks above
            return False

    def _schema_problems(self, record: ActionRecord) -> list[str]:
        """Whether the table still has the columns the reviewer was shown."""
        try:
            changes = classify(SqlAction(record.sql_normalized or "", record.dialect))
            with self._lock:
                plan = self.guard.adapter.plan_schema_change(
                    record.table, changes.statements[0].schema_change or ()
                )
        except Exception:  # noqa: BLE001 - a column that vanished must not hide the page
            _log.warning("could not plan action %s again", record.id, exc_info=True)
            return ["the table's columns could not be checked against the review"]
        if schema_plan_digest(plan) != record.diff_digest:
            return [
                "the table's columns differ from the ones that were reviewed; approving would "
                "end as stale. Reject it, or let it expire."
            ]
        return []

    def _diff(self, record: ActionRecord) -> tuple[RowDiff | None, str | None]:
        with self._lock:  # also guards the small cache below
            return self._diff_locked(record)

    def _diff_locked(self, record: ActionRecord) -> tuple[RowDiff | None, str | None]:
        key = (record.id, record.base_snapshot_id, record.head_snapshot_id, record.diff_digest)
        cached = self._diffs.get(key)
        if cached is not None and time.monotonic() - cached.at < DIFF_CACHE_SECONDS:
            diff = cached.diff
        else:
            try:
                diff = self.guard.diff_for(record.id)
            except Exception as err:  # noqa: BLE001 - the Guard's DiffError text is safe to show
                _log.warning("row diff of action %s failed", record.id, exc_info=True)
                detail = str(err) if type(err).__name__ in ("DiffError", "DiffTooLarge") else ""
                return None, "the row diff could not be recomputed" + (
                    f": {detail}" if detail else ""
                )
            self._remember(key, diff)
        if diff.digest != record.diff_digest:
            return diff, (
                "the row diff recomputed from the snapshots differs from the one that was "
                "reviewed; do not approve"
            )
        return diff, None

    def _remember(self, key: tuple[Any, ...], diff: RowDiff) -> None:
        if len(self._diffs) >= DIFF_CACHE_ENTRIES:
            self._diffs.pop(min(self._diffs, key=lambda k: self._diffs[k].at))
        self._diffs[key] = _Cached(time.monotonic(), diff)

    def _invariants(self, table: str) -> dict[str, Any]:
        configured = self.guard.policy.invariants.for_table(table)
        return {
            name: value
            for name in (
                "max_delete_pct",
                "max_row_decrease_pct",
                "min_rows",
                "max_rows",
                "not_null",
                "unique_key",
            )
            if (value := getattr(configured, name)) not in (None, ())
        }

    @staticmethod
    def _help(record: ActionRecord) -> tuple[str, ...]:
        if record.state is not ActionState.NEEDS_ATTENTION:
            return ()
        notes = [NEEDS_ATTENTION_HELP]
        reason = str((record.outcome_data or {}).get("reason", ""))
        if _CANNOT_PROVE_GONE in reason:
            notes.append(CANNOT_PROVE_GONE_HELP)
            if sys.platform == "darwin":
                notes.append("This machine is a Mac, so this is the expected outcome of a crash.")
        return tuple(notes)

    # -- deciding ----------------------------------------------------------------------------

    def decide(
        self, action_id: str, decision: Decision, proof: str, note: str | None = None
    ) -> Decided:
        """Apply a decision through `Guard.resolve`; rejecting needs a note.

        Raises ReviewError for a bad note, and the Guard's `UnknownAction`, `ActionNotPending`
        and `ProofRejected` unchanged (nothing happened in those cases). The note goes into the
        audit log next to what the decision led to.
        """
        text = _note(note)
        if decision == "reject" and not text:
            raise ReviewError("a note is required to reject an action")
        with self._lock:
            result = self.guard.resolve(action_id, decision, self.approver, proof)
        recorded = True
        if text:
            try:
                self.guard.audit.append(
                    {
                        "phase": "completed",
                        "outcome": "reviewer_note",
                        "action_id": action_id,
                        "decision": decision,
                        "approver": self.approver,
                        "resolved": result.outcome.value,
                        "note": text,
                    }
                )
            except AuditError:
                _log.error(
                    "the note for action %s could not be written to the audit log", action_id
                )
                recorded = False
        return Decided(result, recorded)

    def decide_with_new_proof(
        self, action_id: str, decision: Decision, note: str | None = None
    ) -> Decided:
        """Decide with a proof minted now: for a front end where the person just confirmed."""
        record = self.find(action_id)
        return self.decide(
            record.id, decision, self.proofs.mint(self._request(record, decision)), note
        )

    # -- housekeeping ------------------------------------------------------------------------

    def housekeeping(self) -> HousekeepingStatus:
        """Settle crashed publishes, expire late actions, sweep orphaned branches.

        Safe to call often: a crashed publish keeps its lease for several minutes, so the run
        that follows the crash skips it and a later one settles it. Failures are recorded in the
        status (without the error text) and logged, never raised.
        """
        settled = expired = 0
        error: str | None = None
        try:
            with self._housekeeping_lock:  # not `_lock`: the store's compare-and-set keeps it safe
                settled = len(self.guard.reconcile())
                expired = len(self.guard.expire_pending())
        except Exception as err:  # noqa: BLE001 - the loop must survive any one failed pass
            _log.warning("housekeeping failed", exc_info=True)
            error = type(err).__name__
        self._status = HousekeepingStatus(self._clock(), error, settled, expired)
        return self._status

    @property
    def housekeeping_status(self) -> HousekeepingStatus:
        return self._status

    # -- audit -------------------------------------------------------------------------------

    def audit_tail(self, limit: int = AUDIT_TAIL) -> list[dict[str, Any]]:
        """The last `limit` audit records, newest first, reduced to what a person needs."""
        path = self.guard.audit.path
        if not path.exists():
            return []
        lines: deque[str] = deque(maxlen=limit)
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if line.strip():
                    lines.append(line)
        entries: list[dict[str, Any]] = []
        for line in reversed(lines):
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict):
                entries.append(_audit_entry(record))
        return entries


def _note(note: str | None) -> str:
    text = "".join(ch for ch in (note or "") if ch.isprintable() or ch in "\n\t").strip()
    if len(text) > MAX_NOTE_CHARS:
        raise ReviewError(f"the note is longer than {MAX_NOTE_CHARS} characters")
    return text


def _audit_entry(record: dict[str, Any]) -> dict[str, Any]:
    action = record.get("action")
    described = ""
    if isinstance(action, dict):
        if action.get("type") == "sql":
            # the log may hold raw SQL (a Guard can be built to keep it); never display it
            described = redact_sql(str(action.get("sql", "")), str(action.get("dialect", "duckdb")))
        else:
            described = f"{action.get('type', '')} {action.get('table', '')}".strip()
    reasons = record.get("reasons")
    return {
        "ts": str(record.get("ts", "")),
        "phase": str(record.get("phase", "")),
        "outcome": str(record.get("outcome") or ""),
        "decision": str(record.get("decision") or ""),
        "approver": str(record.get("approver") or ""),
        "action_id": str(record.get("action_id") or ""),
        "what": described[:300],
        "note": str(record.get("note") or "")[:MAX_NOTE_CHARS],
        "reasons": [str(r)[:300] for r in reasons][:5] if isinstance(reasons, list) else [],
        "needs_attention": bool(record.get("needs_attention")),
    }


__all__ = [
    "Counts",
    "Decided",
    "DetailView",
    "HousekeepingStatus",
    "QueueRow",
    "QueueView",
    "ReviewError",
    "ReviewService",
    "approver_label",
    "counts_of",
]
