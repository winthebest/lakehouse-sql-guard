"""Durable store for actions that wait for a human: one SQLite file, one row per action.

The Guard rehearses an action on an Iceberg branch, writes a row here in state `pending`, and
returns its id. Someone with the right proof later approves or rejects it (`Guard.resolve`).
This module is only persistence and the state machine; every decision lives in the Guard.

    pending -> publishing -> published | stale | failed | needs_attention
    pending -> rejected | expired | stale

A transition is a single `UPDATE ... WHERE id = ? AND state = ?`, so two processes racing for the
same action cannot both win. The file is opened in WAL mode and every write runs under
`BEGIN IMMEDIATE`; it is created with mode 0600 because pending rows hold raw SQL.

Nothing secret is stored: not the approval nonce, not the HMAC key. `used_proofs` keeps only a
hash of each proof that was accepted, so a proof cannot be replayed.

When an action reaches a terminal state the raw SQL (`sql_normalized`) and the detailed
maintenance plan (`maintenance_json`, which lists files) are erased. What stays is the redacted
SQL, its sha256, the digests, and the counts in `impact_json` / `outcome_json`. No row values are
ever stored. `secure_delete` and a WAL truncate make the erasure physical, not just logical.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, fields, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlguard.safety.actions import (
    DropTable,
    ExpireSnapshots,
    FastForward,
    MaintenanceAction,
    RemoveOrphanFiles,
    RollbackToSnapshot,
)

if TYPE_CHECKING:
    from sqlguard.safety.executor.base import MaintenancePlan, SchemaPlan

# `ActionRecord.kind` of a column drop or rename: no branch and no row diff
ALTER_KIND = "alter_table"
# 1 lacked owner, lease_until, drop_pending (and the display fields in the hash); 2 lacked the
# `alter_table` kind. 3 has the same columns as 2, but a build that does not know the kind must
# not open a store that may hold such a row, so it refuses the file.
SCHEMA_VERSION = 3
_TS_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"  # fixed width, so text order is time order


class StoreError(RuntimeError):
    """The action store cannot be used (unreadable file, newer schema, I/O failure)."""


class InvalidTransition(StoreError):
    """A state change the state machine does not allow (a programming error, not a race)."""


class ProofReplayed(StoreError):
    """The proof was already used for an earlier decision."""


class ActionState(StrEnum):
    PENDING = "pending"  # rehearsed, waiting for a decision
    PUBLISHING = "publishing"  # approved; the change is being applied to main
    PUBLISHED = "published"  # applied and verified
    STALE = "stale"  # live state, policy or diff differ from what was reviewed; nothing applied
    FAILED = "failed"  # applying failed, or was applied and undone; main is as before
    NEEDS_ATTENTION = "needs_attention"  # main may hold an unverified change; a person must look
    REJECTED = "rejected"
    EXPIRED = "expired"


S = ActionState
TERMINAL_STATES = frozenset(
    {S.PUBLISHED, S.STALE, S.FAILED, S.NEEDS_ATTENTION, S.REJECTED, S.EXPIRED}
)
_TRANSITIONS: dict[ActionState, frozenset[ActionState]] = {
    S.PENDING: frozenset({S.PUBLISHING, S.REJECTED, S.EXPIRED, S.STALE}),
    S.PUBLISHING: frozenset({S.PUBLISHED, S.STALE, S.FAILED, S.NEEDS_ATTENTION}),
}


def canonical_json(value: Any) -> str:
    """Stable JSON text: sorted keys, no whitespace. Same value, same text, in any process."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def digest(value: Any) -> str:
    """sha256 of the canonical JSON of `value`."""
    return sha256_hex(canonical_json(value))


def proof_hash(proof: str) -> str:
    """What is remembered of an accepted proof (never the proof itself)."""
    return sha256_hex("sqlguard-proof\0" + proof)


_MAINTENANCE_KINDS: dict[str, type[MaintenanceAction]] = {
    "expire_snapshots": ExpireSnapshots,
    "remove_orphan_files": RemoveOrphanFiles,
    "rollback": RollbackToSnapshot,
    "fast_forward": FastForward,
    "drop_table": DropTable,
}
_KIND_OF = {cls: name for name, cls in _MAINTENANCE_KINDS.items()}
_DATETIME_FIELDS = frozenset({"older_than", "as_of"})


def maintenance_kind(action: MaintenanceAction) -> str:
    return _KIND_OF.get(type(action), type(action).__name__.lower())


def encode_action(action: MaintenanceAction) -> dict[str, Any]:
    """A maintenance action as plain JSON data; `decode_action` restores it exactly."""
    data: dict[str, Any] = {"kind": maintenance_kind(action)}
    for f in fields(action):
        value = getattr(action, f.name)
        if isinstance(value, datetime):
            value = value.isoformat()
        elif isinstance(value, tuple):
            value = list(value)
        data[f.name] = value
    return data


def decode_action(data: Mapping[str, Any]) -> MaintenanceAction:
    cls = _MAINTENANCE_KINDS.get(str(data.get("kind")))
    if cls is None:
        raise StoreError(f"unknown maintenance action kind {data.get('kind')!r}")
    values: dict[str, Any] = {}
    for f in fields(cls):
        if f.name not in data:
            continue
        value = data[f.name]
        if f.name in _DATETIME_FIELDS and value is not None:
            value = datetime.fromisoformat(value)
        elif isinstance(value, list):
            value = tuple(value)
        values[f.name] = value
    return cls(**values)


def plan_digest(plan: MaintenancePlan) -> str:
    """Fingerprint of everything a maintenance plan would touch, for re-plan equivalence."""
    return digest(
        {
            "kind": plan.kind,
            "table": plan.table,
            "irreversible": plan.irreversible,
            "expired": sorted((s.snapshot_id, s.timestamp_ms) for s in plan.expired),
            "remaining_snapshots": plan.remaining_snapshots,
            "orphans": sorted((f.location, f.size) for f in plan.orphan_files),
            "current": plan.current_snapshot_id,
            "target": plan.target_snapshot_id,
            "warnings": list(plan.warnings),
        }
    )


def schema_plan_digest(plan: SchemaPlan) -> str:
    """Fingerprint of a schema change and the state it was planned on, for re-plan equivalence."""
    return digest(plan.to_dict())


def _stamp(moment: datetime) -> str:
    aware = moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)
    return aware.astimezone(UTC).strftime(_TS_FORMAT)


def _parse_stamp(text: str | None) -> datetime | None:
    return None if text is None else datetime.strptime(text, _TS_FORMAT).replace(tzinfo=UTC)


@dataclass(frozen=True)
class ActionRecord:
    """One row of `actions`. JSON columns stay text; the `*_data` properties parse them."""

    id: str
    created_at: datetime
    expires_at: datetime
    state: ActionState
    # "sql", `ALTER_KIND`, or the maintenance action name (expire_snapshots, rollback, ...)
    kind: str
    dialect: str
    sql_normalized: str | None  # erased at a terminal state
    sql_redacted: str
    sql_sha256: str
    maintenance_json: str | None  # {"action": ..., "plan": ...}; erased at a terminal state
    scope_json: str  # the task scope given at submission ("null" when there was none)
    catalog_name: str
    engine: str
    table: str
    base_snapshot_id: int | None
    head_snapshot_id: int | None  # the branch head that was rehearsed, or a pointer move's target
    branch: str | None
    policy_digest: str
    invariants_digest: str
    diff_digest: str  # row diff base->head (SQL) or the maintenance plan (maintenance)
    action_hash: str
    impact_json: str
    reasons_json: str
    requester: str
    approver: str | None = None
    decided_at: datetime | None = None
    outcome_json: str | None = None
    owner: str | None = None  # who is publishing it: "host|boot|pidns|pid|token" (when claimed)
    lease_until: datetime | None = None  # the owner promises to be alive until then
    drop_pending: int = 0  # 1 while the audit branch of a finished action still has to be dropped

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    @property
    def scope_data(self) -> dict[str, Any] | None:
        return json.loads(self.scope_json)

    @property
    def maintenance_data(self) -> dict[str, Any] | None:
        return None if self.maintenance_json is None else json.loads(self.maintenance_json)

    @property
    def impact_data(self) -> dict[str, Any]:
        return json.loads(self.impact_json)

    @property
    def reasons(self) -> list[str]:
        return json.loads(self.reasons_json)

    @property
    def outcome_data(self) -> dict[str, Any] | None:
        return None if self.outcome_json is None else json.loads(self.outcome_json)

    def hash_inputs(self) -> dict[str, Any]:
        """Exactly what `action_hash` covers: what is decided on and what is shown about it."""
        maintenance = self.maintenance_data
        return {
            "v": 2,
            "sql_redacted": self.sql_redacted,
            "impact": self.impact_json,
            "reasons": self.reasons_json,
            "branch": self.branch,
            "expires_at": _stamp(self.expires_at),
            "kind": self.kind,
            "dialect": self.dialect,
            "sql": self.sql_normalized,
            "maintenance": None if maintenance is None else maintenance.get("action"),
            "table": self.table,
            "catalog": self.catalog_name,
            "engine": self.engine,
            "base": self.base_snapshot_id,
            "head": self.head_snapshot_id,
            "policy_digest": self.policy_digest,
            "invariants_digest": self.invariants_digest,
            "diff_digest": self.diff_digest,
            "requester": self.requester,
            "scope": self.scope_data,
        }

    def computed_hash(self) -> str:
        return digest(self.hash_inputs())

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly view for a UI or CLI. Raw SQL is included only while it is still stored."""
        out: dict[str, Any] = {}
        for f in fields(self):
            value = getattr(self, f.name)
            if isinstance(value, datetime):
                value = _stamp(value)
            elif isinstance(value, ActionState):
                value = value.value
            out[f.name] = value
        for name in (
            "scope_json",
            "maintenance_json",
            "impact_json",
            "reasons_json",
            "outcome_json",
        ):
            text = out.pop(name)
            out[name.removesuffix("_json")] = None if text is None else json.loads(text)
        return out


_COLUMNS = tuple(f.name for f in fields(ActionRecord))
_V2_COLUMNS = {
    "owner": "TEXT",
    "lease_until": "TEXT",
    "drop_pending": "INTEGER NOT NULL DEFAULT 0",
}
_ERASE = ("sql_normalized = NULL", "maintenance_json = NULL")
_DROP_PENDING = "drop_pending = CASE WHEN branch IS NULL THEN 0 ELSE 1 END"
_SCHEMA = """
CREATE TABLE IF NOT EXISTS actions (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    state TEXT NOT NULL,
    kind TEXT NOT NULL,
    dialect TEXT NOT NULL,
    sql_normalized TEXT,
    sql_redacted TEXT NOT NULL,
    sql_sha256 TEXT NOT NULL,
    maintenance_json TEXT,
    scope_json TEXT NOT NULL,
    catalog_name TEXT NOT NULL,
    engine TEXT NOT NULL,
    "table" TEXT NOT NULL,
    base_snapshot_id INTEGER,
    head_snapshot_id INTEGER,
    branch TEXT,
    policy_digest TEXT NOT NULL,
    invariants_digest TEXT NOT NULL,
    diff_digest TEXT NOT NULL,
    action_hash TEXT NOT NULL,
    impact_json TEXT NOT NULL,
    reasons_json TEXT NOT NULL,
    requester TEXT NOT NULL,
    approver TEXT,
    decided_at TEXT,
    outcome_json TEXT,
    owner TEXT,
    lease_until TEXT,
    drop_pending INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS actions_state ON actions (state, expires_at);
CREATE TABLE IF NOT EXISTS used_proofs (
    proof_hash TEXT PRIMARY KEY,
    used_at TEXT NOT NULL,
    action_id TEXT NOT NULL
);
"""
_SELECT = ", ".join(f'"{c}"' for c in _COLUMNS)


def _record(row: sqlite3.Row) -> ActionRecord:
    values = dict(zip(_COLUMNS, tuple(row), strict=True))
    for name in ("created_at", "expires_at", "decided_at", "lease_until"):
        values[name] = _parse_stamp(values[name])
    values["state"] = ActionState(values["state"])
    return ActionRecord(**values)


class ActionStore:
    """SQLite-backed actions. Safe to use from several threads and processes at once.

    Every method opens its own short-lived connection, so no connection is ever shared.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
            os.close(fd)
            os.chmod(self.path, 0o600)
            with self._tx() as conn:
                self._prepare(conn)
        except (OSError, sqlite3.Error) as err:
            raise StoreError(f"cannot open action store {self.path}: {err}") from err

    def _prepare(self, conn: sqlite3.Connection) -> None:
        """Create the schema, upgrade an older store in place, or refuse one it cannot read.

        Version 0 is a new file (an unversioned file that already has tables is refused).
        Version 1 is upgraded by adding the lease and branch-drop columns; its pending
        actions keep their rows but were hashed without the display fields, so they no longer
        match their `action_hash` and `resolve` ends them as `stale` (nothing is re-blessed
        by an upgrade). Version 2 only needs its version stamp raised (no column changed). A
        store written by a newer build is refused.
        """
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        has_actions = (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'actions'"
            ).fetchone()
            is not None
        )
        if version > SCHEMA_VERSION:
            raise StoreError(
                f"action store {self.path} has schema version {version}; "
                f"this build understands up to {SCHEMA_VERSION}"
            )
        if version == 0 and has_actions:
            raise StoreError(f"action store {self.path} has tables but no schema version")
        if version == 1:
            have = {row[1] for row in conn.execute("PRAGMA table_info(actions)").fetchall()}
            for column, ddl in _V2_COLUMNS.items():
                if column not in have:
                    conn.execute(f"ALTER TABLE actions ADD COLUMN {column} {ddl}")
        for statement in _SCHEMA.strip().split(";"):
            if statement.strip():
                conn.execute(statement)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    # -- connections -----------------------------------------------------------------------------

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = FULL")  # a `publishing` marker must survive a crash
        conn.execute("PRAGMA secure_delete = ON")  # erased SQL is overwritten, not left in pages
        return conn

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        conn = self._open()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        finally:
            conn.close()

    def scrub(self) -> bool:
        """Fold the WAL into the main file and empty it, so erased rows leave no copy behind.

        Returns False when another connection holds a read transaction and the WAL could not be
        emptied; the erased bytes then linger in `-wal` until a later call (the Guard repeats it
        on `expire_pending` and `reconcile`) or until the last connection closes.
        """
        conn = self._open()
        try:
            conn.execute("PRAGMA busy_timeout = 100")  # a reader should not stall a decision
            busy, log, done = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            return busy == 0 and log <= done
        except sqlite3.Error:
            return False
        finally:
            conn.close()

    _scrub = scrub

    # -- reads -----------------------------------------------------------------------------------

    def get(self, action_id: str) -> ActionRecord | None:
        conn = self._open()
        try:
            row = conn.execute(
                f"SELECT {_SELECT} FROM actions WHERE id = ?", (action_id,)
            ).fetchone()
        except sqlite3.Error as err:
            raise StoreError(f"cannot read action {action_id}: {err}") from err
        finally:
            conn.close()
        return None if row is None else _record(row)

    def list_actions(
        self, states: Sequence[ActionState] | None = None, limit: int = 200
    ) -> list[ActionRecord]:
        """Newest first, optionally only in `states`."""
        where, params = "", []
        if states:
            where = f"WHERE state IN ({', '.join('?' for _ in states)})"
            params = [s.value for s in states]
        conn = self._open()
        try:
            rows = conn.execute(
                f"SELECT {_SELECT} FROM actions {where} ORDER BY created_at DESC, id LIMIT ?",
                (*params, limit),
            ).fetchall()
        except sqlite3.Error as err:
            raise StoreError(f"cannot list actions: {err}") from err
        finally:
            conn.close()
        return [_record(r) for r in rows]

    def proof_used(self, hashed: str) -> bool:
        conn = self._open()
        try:
            return (
                conn.execute("SELECT 1 FROM used_proofs WHERE proof_hash = ?", (hashed,)).fetchone()
                is not None
            )
        finally:
            conn.close()

    # -- writes ----------------------------------------------------------------------------------

    def insert(self, record: ActionRecord) -> None:
        """Add a new action; it must be `pending`."""
        if record.state is not S.PENDING:
            raise InvalidTransition("a new action starts as pending")
        values = {
            name: _stamp(v) if isinstance(v := getattr(record, name), datetime) else v
            for name in _COLUMNS
        }
        values["state"] = record.state.value
        marks = ", ".join("?" for _ in _COLUMNS)
        try:
            with self._tx() as conn:
                conn.execute(
                    f"INSERT INTO actions ({_SELECT}) VALUES ({marks})",
                    [values[c] for c in _COLUMNS],
                )
        except sqlite3.Error as err:
            raise StoreError(f"cannot store action {record.id}: {err}") from err

    def transition(
        self,
        action_id: str,
        expected: ActionState,
        new: ActionState,
        *,
        now: datetime,
        approver: str | None = None,
        outcome: Mapping[str, Any] | None = None,
        owner: str | None = None,
        keep_branch: bool = False,
    ) -> bool:
        """Move `expected` -> `new`; True only for the caller whose update changed the row.

        A terminal `new` erases the raw SQL and the detailed maintenance plan, and marks the
        branch as still to be dropped (`drop_pending`) unless `keep_branch`. With `owner`, the
        update applies only while that owner still holds the row. `PENDING -> PUBLISHING` and
        the decision `PENDING -> REJECTED` go through `claim`, which also consumes the proof.
        """
        self._check_edge(expected, new)
        sets = ["state = ?", "decided_at = COALESCE(decided_at, ?)"]
        params: list[Any] = [new.value, _stamp(now)]
        if approver is not None:
            sets.append("approver = ?")
            params.append(approver)
        if outcome is not None:
            sets.append("outcome_json = ?")
            params.append(canonical_json(outcome))
        if new in TERMINAL_STATES:
            sets.extend(_ERASE)
            if not keep_branch:
                sets.append(_DROP_PENDING)
        where, where_params = "id = ? AND state = ?", [action_id, expected.value]
        if owner is not None:
            where += " AND owner = ?"
            where_params.append(owner)
        try:
            with self._tx() as conn:
                changed = conn.execute(
                    f"UPDATE actions SET {', '.join(sets)} WHERE {where}", (*params, *where_params)
                ).rowcount
        except sqlite3.Error as err:
            raise StoreError(f"cannot update action {action_id}: {err}") from err
        if new in TERMINAL_STATES:
            self._scrub()
        return changed == 1

    def claim(
        self,
        action_id: str,
        new: ActionState,
        *,
        proof: str,
        approver: str,
        now: datetime,
        outcome: Mapping[str, Any] | None = None,
        owner: str | None = None,
        lease_until: datetime | None = None,
    ) -> bool:
        """Decide a pending action in one transaction: `pending -> new` and the proof is spent.

        `new` is `PUBLISHING` (approve; `owner` and `lease_until` record who is publishing and
        until when they promise to be alive) or `REJECTED`. Returns False, changing nothing, when
        the action is no longer pending or has expired. Raises ProofReplayed (also changing
        nothing) when the proof was used before. Either both effects happen or neither.
        """
        if new not in (S.PUBLISHING, S.REJECTED):
            raise InvalidTransition(
                f"a decision moves an action to publishing or rejected, not {new}"
            )
        sets = ["state = ?", "approver = ?", "decided_at = ?"]
        params: list[Any] = [new.value, approver, _stamp(now)]
        if outcome is not None:
            sets.append("outcome_json = ?")
            params.append(canonical_json(outcome))
        if new is S.PUBLISHING:
            sets.extend(("owner = ?", "lease_until = ?"))
            params.extend((owner, None if lease_until is None else _stamp(lease_until)))
        if new in TERMINAL_STATES:
            sets.extend((*_ERASE, _DROP_PENDING))
        try:
            with self._tx() as conn:
                changed = conn.execute(
                    f"UPDATE actions SET {', '.join(sets)} "
                    "WHERE id = ? AND state = 'pending' AND expires_at > ?",
                    (*params, action_id, _stamp(now)),
                ).rowcount
                if changed != 1:
                    return False
                try:
                    conn.execute(
                        "INSERT INTO used_proofs (proof_hash, used_at, action_id) VALUES (?, ?, ?)",
                        (proof_hash(proof), _stamp(now), action_id),
                    )
                except sqlite3.IntegrityError as err:
                    raise ProofReplayed("this proof was already used") from err
        except sqlite3.Error as err:
            raise StoreError(f"cannot decide action {action_id}: {err}") from err
        if new in TERMINAL_STATES:
            self._scrub()
        return True

    def expire_due(
        self, now: datetime, *, catalog_name: str | None = None, engine: str | None = None
    ) -> list[ActionRecord]:
        """Mark pending actions whose time is up as expired; returns the rows it expired.

        With `catalog_name` / `engine`, only actions queued for that catalog and engine.
        """
        scope, scope_params = "", []
        if catalog_name is not None:
            scope += " AND catalog_name = ?"
            scope_params.append(catalog_name)
        if engine is not None:
            scope += " AND engine = ?"
            scope_params.append(engine)
        try:
            with self._tx() as conn:
                rows = conn.execute(
                    f"SELECT {_SELECT} FROM actions "
                    f"WHERE state = 'pending' AND expires_at <= ?{scope}",
                    (_stamp(now), *scope_params),
                ).fetchall()
                if rows:
                    conn.execute(
                        f"UPDATE actions SET state = 'expired', decided_at = ?, "
                        f"{', '.join(_ERASE)}, {_DROP_PENDING} "
                        f"WHERE state = 'pending' AND expires_at <= ?{scope}",
                        (_stamp(now), _stamp(now), *scope_params),
                    )
        except sqlite3.Error as err:
            raise StoreError(f"cannot expire actions: {err}") from err
        if rows:
            self._scrub()
        return [
            replace(_record(r), state=S.EXPIRED, sql_normalized=None, maintenance_json=None)
            for r in rows
        ]

    def renew_lease(self, action_id: str, owner: str, lease_until: datetime) -> bool:
        """Extend `owner`'s hold on a publishing action; False once someone else holds it."""
        return self._cas(
            "UPDATE actions SET lease_until = ? "
            "WHERE id = ? AND state = 'publishing' AND owner = ?",
            (_stamp(lease_until), action_id, owner),
        )

    def take_over(
        self,
        action_id: str,
        *,
        expected_owner: str | None,
        new_owner: str,
        now: datetime,
        lease_until: datetime,
    ) -> bool:
        """Become the owner of a publishing action whose lease has run out (compare-and-set)."""
        return self._cas(
            "UPDATE actions SET owner = ?, lease_until = ? WHERE id = ? AND state = 'publishing' "
            "AND owner IS ? AND (lease_until IS NULL OR lease_until <= ?)",
            (new_owner, _stamp(lease_until), action_id, expected_owner, _stamp(now)),
        )

    def mark_dropped(self, action_id: str) -> None:
        self._cas("UPDATE actions SET drop_pending = 0 WHERE id = ?", (action_id,))

    def list_drop_pending(
        self, *, catalog_name: str | None = None, engine: str | None = None, limit: int = 500
    ) -> list[ActionRecord]:
        """Finished actions whose audit branch has not been dropped yet."""
        scope, params = "", []
        if catalog_name is not None:
            scope += " AND catalog_name = ?"
            params.append(catalog_name)
        if engine is not None:
            scope += " AND engine = ?"
            params.append(engine)
        conn = self._open()
        try:
            rows = conn.execute(
                f"SELECT {_SELECT} FROM actions WHERE drop_pending = 1{scope} LIMIT ?",
                (*params, limit),
            ).fetchall()
        except sqlite3.Error as err:
            raise StoreError(f"cannot list branches to drop: {err}") from err
        finally:
            conn.close()
        return [_record(r) for r in rows]

    def count_pending(self, requester: str | None = None) -> int:
        """Pending actions, optionally only those that `requester` submitted."""
        where, params = "", []
        if requester is not None:
            where, params = " AND requester = ?", [requester]
        conn = self._open()
        try:
            return int(
                conn.execute(
                    f"SELECT count(*) FROM actions WHERE state = 'pending'{where}", params
                ).fetchone()[0]
            )
        finally:
            conn.close()

    def live_branches(self) -> set[str]:
        """Branch names that must not be swept: unfinished actions', and those of
        `needs_attention` actions, whose branch is what a person was asked to inspect."""
        conn = self._open()
        try:
            rows = conn.execute(
                "SELECT branch FROM actions WHERE branch IS NOT NULL "
                "AND state IN ('pending', 'publishing', 'needs_attention')"
            ).fetchall()
        finally:
            conn.close()
        return {r[0] for r in rows}

    def action_tables(
        self, *, catalog_name: str | None = None, engine: str | None = None
    ) -> set[str]:
        """Tables that any action (finished or not) was ever queued for."""
        scope, params = "", []
        if catalog_name is not None:
            scope += " AND catalog_name = ?"
            params.append(catalog_name)
        if engine is not None:
            scope += " AND engine = ?"
            params.append(engine)
        conn = self._open()
        try:
            rows = conn.execute(
                f'SELECT DISTINCT "table" FROM actions WHERE 1 = 1{scope}', params
            ).fetchall()
        finally:
            conn.close()
        return {r[0] for r in rows}

    def _cas(self, statement: str, params: Sequence[Any]) -> bool:
        try:
            with self._tx() as conn:
                return conn.execute(statement, params).rowcount == 1
        except sqlite3.Error as err:
            raise StoreError(f"cannot update the action store: {err}") from err

    @staticmethod
    def _check_edge(expected: ActionState, new: ActionState) -> None:
        if new not in _TRANSITIONS.get(expected, frozenset()):
            raise InvalidTransition(f"{expected.value} -> {new.value} is not a valid transition")
