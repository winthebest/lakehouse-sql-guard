"""Durable pending actions: the store, the state machine, `Guard.resolve` and `Guard.reconcile`."""

from __future__ import annotations

import hashlib
import hmac
import os
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pyarrow as pa
import pytest

from sqlguard.core.findings import Decision
from sqlguard.safety import (
    ActionNotPending,
    ActionRecord,
    ActionState,
    ActionStore,
    DiffLimits,
    ExpireSnapshots,
    Guard,
    InvalidTransition,
    Policy,
    ProofRejected,
    ProofRequest,
    RollbackToSnapshot,
    SqlAction,
    UnknownAction,
)
from sqlguard.safety.guard import Outcome
from sqlguard.safety.invariants import Invariant
from sqlguard.safety.store import ProofReplayed, digest, proof_hash
from tests.safety.conftest import ROWS, main_ids, order_ids

RISKY = "DELETE FROM orders WHERE id <= 15"  # 75% of the rows: rehearsal says ask
SECRET_LITERAL = "zz-secret-7731"
APPROVER = "alice"
A = ActionState


class Clock:
    def __init__(self) -> None:
        self.now = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


class HmacVerifier:
    """Test stand-in for the real verifier: an HMAC over the exact request, with a nonce."""

    def __init__(self, secret: bytes = b"test-secret") -> None:
        self.secret = secret

    def proof(self, request: ProofRequest, nonce: str = "n1") -> str:
        message = "|".join(
            (
                request.action_id,
                request.action_hash,
                request.decision,
                request.approver_label,
                nonce,
            )
        )
        return f"{nonce}.{hmac.new(self.secret, message.encode(), hashlib.sha256).hexdigest()}"

    def verify(self, request: ProofRequest, proof: str) -> bool:
        nonce, _, _ = proof.partition(".")
        return hmac.compare_digest(self.proof(request, nonce), proof)


class AcceptAnything:
    def verify(self, request: ProofRequest, proof: str) -> bool:
        return True


class Crash(BaseException):
    """Stands in for the process being killed: nothing in the Guard may catch it."""


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def verifier() -> HmacVerifier:
    return HmacVerifier()


@pytest.fixture
def store_path(tmp_path: Path) -> Path:
    return tmp_path / "state" / "actions.db"


@pytest.fixture
def pguard(make_guard, store_path, verifier, clock) -> Guard:
    return make_guard(store=store_path, proof_verifier=verifier, clock=clock)


def one_row(order_id: int) -> pa.Table:
    return pa.table(
        {"id": pa.array([order_id], pa.int64()), "customer": ["x"], "amount": pa.array([1.0])}
    )


def queue(guard: Guard, scope, sql: str = RISKY):
    result = guard.submit(SqlAction(sql), scope)
    assert result.outcome is Outcome.AWAITING_APPROVAL and result.action_id, result.reasons
    return result


def decide(guard, verifier, result, decision="approve", label=APPROVER, nonce="n1"):
    request = ProofRequest(result.action_id, result.action_hash, decision, label)
    return guard.resolve(result.action_id, decision, label, verifier.proof(request, nonce))


def branches(orders) -> set[str]:
    return set(orders.refs("orders")) - {"main"}


def store_bytes(path: Path) -> bytes:
    return b"".join(
        Path(f).read_bytes() for f in (path, f"{path}-wal", f"{path}-shm") if Path(f).exists()
    )


def state_of(guard: Guard, result) -> A:
    return guard.store.get(result.action_id).state


# -- the store -------------------------------------------------------------------------------


def make_record(**over) -> ActionRecord:
    now = datetime.now(UTC)
    base = dict(
        id="a1",
        created_at=now,
        expires_at=now + timedelta(hours=1),
        state=A.PENDING,
        kind="sql",
        dialect="duckdb",
        sql_normalized=f"DELETE FROM orders WHERE customer = '{SECRET_LITERAL}'",
        sql_redacted="DELETE FROM orders WHERE customer = ?",
        sql_sha256="0" * 64,
        maintenance_json=None,
        scope_json="null",
        catalog_name="c",
        engine="duckdb",
        table="db.orders",
        base_snapshot_id=1,
        head_snapshot_id=2,
        branch="sqlguard-x",
        policy_digest="p",
        invariants_digest="i",
        diff_digest="d",
        action_hash="",
        impact_json="{}",
        reasons_json="[]",
        requester="agent",
    )
    base.update(over)
    record = ActionRecord(**base)
    return replace(record, action_hash=record.computed_hash())


def test_the_file_and_its_wal_are_private(store_path):
    store = ActionStore(store_path)
    store.insert(make_record())
    store.transition("a1", A.PENDING, A.REJECTED, now=datetime.now(UTC))
    for suffix in ("", "-wal", "-shm"):
        path = Path(f"{store_path}{suffix}")
        if path.exists():
            assert stat.S_IMODE(path.stat().st_mode) == 0o600, path
    assert stat.S_IMODE(store_path.parent.stat().st_mode) == 0o700
    os.chmod(store_path, 0o644)  # a store that was made too open is tightened on open
    ActionStore(store_path)
    assert stat.S_IMODE(store_path.stat().st_mode) == 0o600


def test_the_store_runs_in_wal_mode(store_path):
    ActionStore(store_path)
    conn = sqlite3.connect(store_path)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    conn.close()


VALID = [
    (A.PENDING, A.PUBLISHING),
    (A.PENDING, A.REJECTED),
    (A.PENDING, A.EXPIRED),
    (A.PENDING, A.STALE),
    (A.PUBLISHING, A.PUBLISHED),
    (A.PUBLISHING, A.STALE),
    (A.PUBLISHING, A.FAILED),
    (A.PUBLISHING, A.NEEDS_ATTENTION),
]


@pytest.mark.parametrize(("old", "new"), VALID)
def test_every_valid_transition_is_allowed(store_path, old, new):
    store = ActionStore(store_path)
    store.insert(make_record())
    if old is A.PUBLISHING:
        assert store.claim("a1", A.PUBLISHING, proof="p", approver="x", now=datetime.now(UTC))
    if new is A.PUBLISHING:
        assert store.claim("a1", new, proof="p", approver="x", now=datetime.now(UTC))
    else:
        assert store.transition("a1", old, new, now=datetime.now(UTC))
    assert store.get("a1").state is new


@pytest.mark.parametrize(
    ("old", "new"),
    [(o, n) for o in A for n in A if (o, n) not in VALID],
)
def test_every_other_transition_is_refused(store_path, old, new):
    store = ActionStore(store_path)
    store.insert(make_record())
    with pytest.raises(InvalidTransition):
        store.transition("a1", old, new, now=datetime.now(UTC))
    assert store.get("a1").state is A.PENDING


def test_a_transition_from_the_wrong_state_changes_nothing(store_path):
    store = ActionStore(store_path)
    store.insert(make_record())
    assert not store.transition("a1", A.PUBLISHING, A.PUBLISHED, now=datetime.now(UTC))
    assert not store.transition("nope", A.PENDING, A.REJECTED, now=datetime.now(UTC))
    assert store.get("a1").state is A.PENDING


def test_terminal_states_cannot_be_left(store_path):
    store = ActionStore(store_path)
    store.insert(make_record())
    assert store.transition("a1", A.PENDING, A.REJECTED, now=datetime.now(UTC))
    for new in A:
        with pytest.raises(InvalidTransition):
            store.transition("a1", A.REJECTED, new, now=datetime.now(UTC))


def test_a_new_action_must_start_pending(store_path):
    with pytest.raises(InvalidTransition):
        ActionStore(store_path).insert(make_record(state=A.PUBLISHED))


def test_two_processes_racing_for_one_action_only_one_wins(store_path):
    store = ActionStore(store_path)
    store.insert(make_record())
    code = (
        "import sys, time\n"
        "from datetime import UTC, datetime\n"
        "from sqlguard.safety.store import ActionState, ActionStore, ProofReplayed\n"
        "path, who, start = sys.argv[1], sys.argv[2], float(sys.argv[3])\n"
        "store = ActionStore(path)\n"
        "while time.time() < start: pass\n"
        "try:\n"
        "    won = store.claim('a1', ActionState.PUBLISHING, proof=who, approver=who,\n"
        "                      now=datetime.now(UTC))\n"
        "except ProofReplayed:\n"
        "    won = False\n"
        "print('won' if won else 'lost')\n"
    )
    start = time.time() + 3
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", code, str(store_path), who, str(start)],
            stdout=subprocess.PIPE,
            text=True,
        )
        for who in ("p1", "p2")
    ]
    results = sorted(p.communicate(timeout=60)[0].strip() for p in procs)
    assert results == ["lost", "won"]
    row = store.get("a1")
    assert row.state is A.PUBLISHING and row.approver in ("p1", "p2")


def test_racing_threads_cannot_both_win_even_with_different_proofs(store_path):
    store = ActionStore(store_path)
    store.insert(make_record())
    barrier = threading.Barrier(8)
    wins: list[bool] = []

    def worker(n: int) -> None:
        own = ActionStore(store_path)
        barrier.wait()
        wins.append(
            own.claim("a1", A.PUBLISHING, proof=f"p{n}", approver="x", now=datetime.now(UTC))
        )

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert wins.count(True) == 1


def test_a_proof_is_spent_in_the_same_transaction_as_the_decision(store_path):
    store = ActionStore(store_path)
    store.insert(make_record())
    store.insert(make_record(id="a2"))
    now = datetime.now(UTC)
    assert store.claim("a1", A.PUBLISHING, proof="same", approver="x", now=now)
    with pytest.raises(ProofReplayed):
        store.claim("a2", A.PUBLISHING, proof="same", approver="x", now=now)
    assert store.get("a2").state is A.PENDING  # the failed claim changed nothing
    assert store.proof_used(proof_hash("same"))
    assert b"same" not in store_bytes(store_path).replace(proof_hash("same").encode(), b"")


def test_an_expired_action_cannot_be_claimed(store_path):
    store = ActionStore(store_path)
    store.insert(make_record())
    late = datetime.now(UTC) + timedelta(hours=2)
    assert not store.claim("a1", A.PUBLISHING, proof="p", approver="x", now=late)
    assert not store.proof_used(proof_hash("p"))
    assert [r.id for r in store.expire_due(late)] == ["a1"]
    assert store.get("a1").state is A.EXPIRED


def test_terminal_rows_keep_no_raw_sql_or_plan_details(store_path):
    store = ActionStore(store_path)
    maintenance = '{"action":{"kind":"x"},"plan":{"orphan_files":["file:///secret/path.parquet"]}}'
    store.insert(make_record(id="m1", maintenance_json=maintenance, sql_normalized=None))
    store.insert(make_record(id="s1"))
    assert SECRET_LITERAL.encode() in store_bytes(store_path)
    now = datetime.now(UTC)
    store.transition("s1", A.PENDING, A.STALE, now=now, outcome={"outcome": "stale"})
    store.transition("m1", A.PENDING, A.EXPIRED, now=now)
    assert SECRET_LITERAL.encode() not in store_bytes(store_path)
    assert b"secret/path" not in store_bytes(store_path)
    row = store.get("s1")
    assert (
        row.sql_normalized is None and row.sql_redacted == "DELETE FROM orders WHERE customer = ?"
    )
    assert store.get("m1").maintenance_json is None


def test_the_store_survives_being_reopened(store_path):
    ActionStore(store_path).insert(make_record())
    assert ActionStore(store_path).get("a1").table == "db.orders"
    assert [r.id for r in ActionStore(store_path).list_actions([A.PENDING])] == ["a1"]
    assert ActionStore(store_path).list_actions([A.PUBLISHED]) == []


# -- digests and hashes ----------------------------------------------------------------------


def test_the_policy_digest_ignores_labels_and_ordering_but_not_rules():
    one = Policy.from_dict({"version": 1, "protected_tables": ["a", "b"], "thresholds": {}})
    two = Policy.from_dict({"version": "renamed", "protected_tables": ["b", "a"]})
    assert one.digest == two.digest and one.version == "1" and two.version == "renamed"
    assert Policy().digest == Policy.from_dict({}).digest
    changed = [
        {"protected_tables": ["a", "b", "orders"]},
        {"thresholds": {"max_affected_row_pct": 5}},
        {"task_scope": {"allowed_tables": ["orders"]}},
        {"retention": {"default": {"min_snapshots": 3}}},
        {"invariants": {"default": {"min_rows": 1}}},
        {"class_decisions": {"reversible_write": "ask"}},
        {"default_namespace": "other"},
    ]
    digests = {Policy.from_dict(c).digest for c in changed}
    assert len(digests) == len(changed) and Policy().digest not in digests


def test_the_action_hash_is_stable_and_covers_every_input():
    record = make_record()
    assert record.action_hash == make_record().computed_hash() == record.computed_hash()
    assert digest({"b": 1, "a": 2}) == digest({"a": 2, "b": 1})
    mutations = {
        "sql_normalized": "DELETE FROM orders",
        "table": "db.other",
        "catalog_name": "elsewhere",
        "engine": "spark",
        "base_snapshot_id": 99,
        "head_snapshot_id": 98,
        "policy_digest": "changed",
        "invariants_digest": "changed",
        "diff_digest": "changed",
        "requester": "someone-else",
        "scope_json": '{"allowed_namespaces":["db"],"allowed_tables":[]}',
        "kind": "rollback",
        "dialect": "spark",
    }
    for name, value in mutations.items():
        assert replace(record, **{name: value}).computed_hash() != record.action_hash, name
    # the redacted text, expiry and bookkeeping are not part of what an approval is bound to
    assert replace(record, sql_redacted="x", state=A.STALE).computed_hash() == record.action_hash


def test_equivalent_spellings_of_a_statement_normalize_to_one_text():
    from sqlguard.safety.guard import normalize_sql

    one = normalize_sql(SqlAction("delete   from orders\n where id<=15 -- why"))
    two = normalize_sql(SqlAction("DELETE FROM orders WHERE id <= 15"))
    assert one == two


# -- queueing --------------------------------------------------------------------------------


def test_a_risky_write_is_rehearsed_saved_and_left_pending(pguard, orders, scope):
    before = main_ids(orders)
    result = pguard.submit(SqlAction(RISKY), scope, requester="agent-7")
    assert result.outcome is Outcome.AWAITING_APPROVAL and result.decision is Decision.ASK
    assert result.executed and not result.published and result.action_id and result.action_hash
    assert result.impact.tables[0].rows_deleted == 15
    assert main_ids(orders) == before and order_ids(orders) == list(range(1, ROWS + 1))

    row = pguard.store.get(result.action_id)
    assert row.state is A.PENDING and row.action_hash == result.action_hash
    assert row.computed_hash() == row.action_hash
    assert row.requester == "agent-7" and row.kind == "sql" and row.table == "db.orders"
    assert row.sql_normalized == "DELETE FROM orders WHERE id <= 15"
    assert row.base_snapshot_id == before[-1] and row.head_snapshot_id != row.base_snapshot_id
    assert row.branch in branches(orders)  # kept for the decision
    assert orders.refs("orders")[row.branch] == row.head_snapshot_id
    assert row.policy_digest == pguard.policy.digest and row.engine == "duckdb"
    assert row.expires_at - row.created_at == timedelta(hours=24)
    assert row.impact_data["impact"]["tables"][0]["rows_deleted"] == 15
    assert row.impact_data["row_diff"]["rows_deleted"] == 15
    assert "rows" not in row.impact_data["row_diff"]  # counts only: no row values are stored
    assert result.to_dict()["action_id"] == result.action_id


def test_without_a_store_awaiting_approval_has_no_action_id(make_guard, orders, scope):
    result = make_guard().submit(SqlAction(RISKY), scope)
    assert result.outcome is Outcome.AWAITING_APPROVAL and result.action_id is None
    assert result.action_hash is None and not branches(orders)


def test_a_synchronous_approver_still_gets_the_v1_path_with_a_store(pguard, orders, scope):
    result = pguard.submit(SqlAction(RISKY), scope, lambda request: True)
    assert result.outcome is Outcome.PUBLISHED and result.action_id is None
    assert pguard.store.list_actions() == [] and not branches(orders)
    declined = pguard.submit(SqlAction("DELETE FROM orders WHERE id <= 18"), scope, lambda r: False)
    assert declined.outcome is Outcome.REJECTED and pguard.store.list_actions() == []


def test_a_write_that_is_allowed_is_not_queued(pguard, orders, scope):
    result = pguard.submit(SqlAction("DELETE FROM orders WHERE id = 1"), scope)
    assert result.outcome is Outcome.PUBLISHED and pguard.store.list_actions() == []


def test_a_denied_write_is_not_queued(pguard, orders):
    result = pguard.submit(SqlAction(RISKY), None)  # no scope: nothing is allowed
    assert result.outcome is Outcome.DENIED and pguard.store.list_actions() == []


def test_a_read_that_needs_approval_is_not_queued(make_guard, store_path, orders, scope):
    guard = make_guard(Policy.from_dict({"class_decisions": {"read": "ask"}}), store=store_path)
    result = guard.submit(SqlAction("SELECT count(*) FROM orders"), scope)
    assert result.outcome is Outcome.AWAITING_APPROVAL and result.action_id is None
    assert guard.store.list_actions() == []


def test_the_tool_backend_reports_a_queued_action_with_its_id(pguard, orders, scope):
    from sqlguard.safety.tools import GuardedBackend

    backend = GuardedBackend(pguard, scope)
    result = backend.call("run_sql", {"sql": RISKY})
    assert result.status == "needs_approval" and result.payload["outcome"] == "awaiting_approval"
    action_id = result.payload["action_id"]
    assert pguard.store.get(action_id).state is A.PENDING
    assert result.guard["action_id"] == action_id
    assert "waiting for a reviewer" in result.payload["message"]
    assert "action_hash" not in result.payload and "action_hash" not in result.guard
    # a guard without a store reports the same status and no id
    plain = GuardedBackend(Guard(orders, audit_path=pguard.audit.path), scope)
    waiting = plain.call("run_sql", {"sql": RISKY})
    assert waiting.status == "needs_approval" and "action_id" not in waiting.payload
    assert waiting.payload["message"].endswith("none was given.")


def test_a_statement_that_cannot_be_rehearsed_is_not_queued(pguard, orders, scope):
    result = pguard.submit(SqlAction("DROP TABLE orders"), scope)
    assert result.outcome is Outcome.UNSUPPORTED and result.action_id is None
    assert pguard.store.list_actions() == [] and orders.has_table("orders")


def test_a_table_too_large_to_diff_is_not_queued_and_leaves_no_branch(
    make_guard, store_path, verifier, orders, scope
):
    guard = make_guard(
        store=store_path, proof_verifier=verifier, diff_limits=DiffLimits(max_rows=5)
    )
    result = guard.submit(SqlAction(RISKY), scope)
    assert result.outcome is Outcome.DENIED and "table too large to diff" in " ".join(
        result.reasons
    )
    assert guard.store.list_actions() == [] and not branches(orders)


def test_the_audit_log_links_the_pending_record_to_its_action(pguard, orders, scope, audit):
    result = queue(pguard, scope)
    done = [r for r in audit.read() if r["phase"] == "completed"][-1]
    assert done["outcome"] == "awaiting_approval" and done["approval"] == "pending"
    assert done["action_id"] == result.action_id
    assert SECRET_LITERAL not in str(audit.read())


# -- resolve: the decision paths -------------------------------------------------------------


def test_approving_publishes_exactly_the_rehearsed_snapshot(pguard, verifier, orders, scope, audit):
    result = queue(pguard, scope)
    row = pguard.store.get(result.action_id)
    done = decide(pguard, verifier, result)
    assert done.outcome is Outcome.PUBLISHED and done.published and done.ok
    assert done.action_id == result.action_id
    assert order_ids(orders) == list(range(16, ROWS + 1))
    assert orders.table("orders").current_snapshot().snapshot_id == row.head_snapshot_id
    assert not branches(orders)
    after = pguard.store.get(result.action_id)
    assert after.state is A.PUBLISHED and after.approver == APPROVER and after.decided_at
    assert after.outcome_data["outcome"] == "published"
    assert after.sql_normalized is None and after.sql_redacted == "DELETE FROM orders WHERE id <= ?"
    assert pguard.store.proof_used(
        proof_hash(
            verifier.proof(ProofRequest(result.action_id, result.action_hash, "approve", APPROVER))
        )
    )
    records = [r for r in audit.read() if r.get("action_id") == result.action_id]
    assert [r["phase"] for r in records] == ["completed", "started", "completed"]
    assert records[-1]["approval"] == "approved" and records[-1]["approver"] == APPROVER


def test_rejecting_drops_the_branch_and_publishes_nothing(pguard, verifier, orders, scope):
    result = queue(pguard, scope)
    before = main_ids(orders)
    done = decide(pguard, verifier, result, "reject")
    assert done.outcome is Outcome.REJECTED and not done.published
    assert main_ids(orders) == before and not branches(orders)
    row = pguard.store.get(result.action_id)
    assert row.state is A.REJECTED and row.sql_normalized is None and row.approver == APPROVER


def test_a_proof_for_another_decision_or_person_is_refused(pguard, verifier, orders, scope):
    result = queue(pguard, scope)
    approve = ProofRequest(result.action_id, result.action_hash, "approve", APPROVER)
    with pytest.raises(ProofRejected):
        pguard.resolve(result.action_id, "reject", APPROVER, verifier.proof(approve))
    with pytest.raises(ProofRejected):
        pguard.resolve(result.action_id, "approve", "mallory", verifier.proof(approve))
    with pytest.raises(ProofRejected):
        pguard.resolve(result.action_id, "approve", APPROVER, "not-a-proof")
    other = queue(pguard, scope, "DELETE FROM orders WHERE id <= 16")
    with pytest.raises(ProofRejected):  # a proof made for a different action
        pguard.resolve(other.action_id, "approve", APPROVER, verifier.proof(approve))
    assert state_of(pguard, result) is A.PENDING and state_of(pguard, other) is A.PENDING
    assert order_ids(orders) == list(range(1, ROWS + 1))
    assert decide(pguard, verifier, result).outcome is Outcome.PUBLISHED  # still decidable


def test_a_failing_or_missing_verifier_refuses_everything(make_guard, store_path, orders, scope):
    class Broken:
        def verify(self, request, proof):
            raise RuntimeError("boom")

    for verifier in (None, Broken()):
        guard = make_guard(store=store_path, proof_verifier=verifier)
        result = guard.submit(SqlAction(RISKY), scope)
        with pytest.raises(ProofRejected):
            guard.resolve(result.action_id, "approve", APPROVER, "anything")
        assert state_of(guard, result) is A.PENDING
        guard.expire_pending(datetime.now(UTC) + timedelta(days=2))


def test_a_verifier_must_return_exactly_true(make_guard, store_path, orders, scope):
    class Sloppy:
        def verify(self, request, proof):
            return "yes"

    guard = make_guard(store=store_path, proof_verifier=Sloppy())
    result = guard.submit(SqlAction(RISKY), scope)
    with pytest.raises(ProofRejected):
        guard.resolve(result.action_id, "approve", APPROVER, "x")


def test_a_used_proof_cannot_decide_another_action(make_guard, store_path, orders, scope):
    guard = make_guard(store=store_path, proof_verifier=AcceptAnything())
    first = guard.submit(SqlAction(RISKY), scope)
    second = guard.submit(SqlAction("DELETE FROM orders WHERE id <= 16"), scope)
    assert (
        guard.resolve(first.action_id, "reject", APPROVER, "one-time").outcome is Outcome.REJECTED
    )
    with pytest.raises(ProofRejected, match="already used"):
        guard.resolve(second.action_id, "approve", APPROVER, "one-time")
    assert state_of(guard, second) is A.PENDING and order_ids(orders) == list(range(1, ROWS + 1))
    assert second.action_id in {r.id for r in guard.store.list_actions([A.PENDING])}


def test_a_decided_or_unknown_action_cannot_be_decided_again(pguard, verifier, orders, scope):
    result = queue(pguard, scope)
    decide(pguard, verifier, result, "reject")
    with pytest.raises(ActionNotPending) as err:
        decide(pguard, verifier, result, "approve", nonce="n2")
    assert err.value.state is A.REJECTED
    with pytest.raises(UnknownAction):
        pguard.resolve("nope", "approve", APPROVER, "p")
    with pytest.raises(ValueError):
        pguard.resolve(result.action_id, "maybe", APPROVER, "p")
    with pytest.raises(ValueError):
        pguard.resolve(result.action_id, "approve", "  ", "p")


def test_a_guard_without_a_store_cannot_resolve(make_guard):
    from sqlguard.safety.guard import PendingActionError

    with pytest.raises(PendingActionError):
        make_guard().resolve("x", "approve", "a", "p")


def test_two_guards_deciding_the_same_action_publish_it_once(
    make_guard, store_path, verifier, clock, orders, scope
):
    one = make_guard(store=store_path, proof_verifier=verifier, clock=clock)
    two = make_guard(store=store_path, proof_verifier=verifier, clock=clock)
    result = queue(one, scope)
    assert decide(one, verifier, result).outcome is Outcome.PUBLISHED
    with pytest.raises(ActionNotPending):
        decide(two, verifier, result, nonce="n2")
    assert order_ids(orders) == list(range(16, ROWS + 1))


# -- resolve: nothing publishes unless the world is what was reviewed -------------------------


def assert_stale(pguard, orders, result, done, *fragments, main_before):
    assert done.outcome is Outcome.STALE and not done.published and not done.ok
    text = " ".join(done.reasons)
    for fragment in fragments:
        assert fragment in text, (fragment, text)
    row = pguard.store.get(result.action_id)
    assert row.state is A.STALE and row.sql_normalized is None
    assert not branches(orders)  # the branch is dropped
    assert main_ids(orders) == main_before


def test_main_written_after_the_review_makes_the_action_stale(
    pguard, verifier, orders, scope, race_on_next_commit
):
    result = queue(pguard, scope)
    orders.table("orders").append(
        pa.table({"id": pa.array([500], pa.int64()), "customer": ["w"], "amount": pa.array([1.0])})
    )
    main = main_ids(orders)
    done = decide(pguard, verifier, result)
    assert_stale(pguard, orders, result, done, "main moved", main_before=main)
    assert 500 in order_ids(orders) and len(order_ids(orders)) == ROWS + 1


def test_the_branch_written_after_the_review_makes_the_action_stale(
    pguard, verifier, orders, scope
):
    result = queue(pguard, scope)
    row = pguard.store.get(result.action_id)
    branch = orders.table("orders")
    branch.append(
        pa.table({"id": pa.array([777], pa.int64()), "customer": ["x"], "amount": pa.array([1.0])}),
        branch=row.branch,
    )
    main = main_ids(orders)
    done = decide(pguard, verifier, result)
    assert_stale(pguard, orders, result, done, "branch moved", main_before=main)
    assert 777 not in order_ids(orders)


def test_a_dropped_branch_makes_the_action_stale(pguard, verifier, orders, scope):
    result = queue(pguard, scope)
    row = pguard.store.get(result.action_id)
    orders.table("orders").manage_snapshots().remove_branch(row.branch).commit()
    done = decide(pguard, verifier, result)
    assert_stale(pguard, orders, result, done, "branch moved", main_before=main_ids(orders))


def test_a_stricter_policy_makes_the_action_stale(
    make_guard, store_path, verifier, clock, orders, scope
):
    result = queue(make_guard(store=store_path, proof_verifier=verifier, clock=clock), scope)
    strict = make_guard(
        Policy(protected_tables=frozenset({"db.orders"})),
        store=store_path,
        proof_verifier=verifier,
        clock=clock,
    )
    done = decide(strict, verifier, result)
    assert_stale(strict, orders, result, done, "policy changed", main_before=main_ids(orders))


def test_a_looser_policy_is_stale_too(make_guard, store_path, verifier, clock, orders, scope):
    result = queue(make_guard(store=store_path, proof_verifier=verifier, clock=clock), scope)
    loose = make_guard(
        Policy(thresholds=Policy().thresholds.__class__(max_affected_row_pct=99)),
        store=store_path,
        proof_verifier=verifier,
        clock=clock,
    )
    done = decide(loose, verifier, result)
    assert done.outcome is Outcome.STALE and "policy changed" in " ".join(done.reasons)


def test_a_policy_scope_that_no_longer_allows_the_table_makes_the_action_stale(
    make_guard, store_path, verifier, clock, orders
):
    # queued under the policy's own scope (no per-call scope), which is then narrowed
    allowing = Policy.from_dict({"task_scope": {"allowed_namespaces": ["db"]}})
    result = queue(
        make_guard(allowing, store=store_path, proof_verifier=verifier, clock=clock), None
    )
    narrowed = Policy.from_dict({"task_scope": {"allowed_tables": ["db.other"]}})
    guard = make_guard(narrowed, store=store_path, proof_verifier=verifier, clock=clock)
    done = decide(guard, verifier, result)
    assert done.outcome is Outcome.STALE and "policy changed" in " ".join(done.reasons)
    assert order_ids(orders) == list(range(1, ROWS + 1))


def edit_row(path: Path, action_id: str, **columns) -> None:
    conn = sqlite3.connect(path)
    sets = ", ".join(f'"{c}" = ?' for c in columns)
    conn.execute(f"UPDATE actions SET {sets} WHERE id = ?", [*columns.values(), action_id])
    conn.commit()
    conn.close()


def test_a_scope_edited_in_the_store_makes_the_action_stale(
    pguard, verifier, orders, scope, store_path
):
    result = queue(pguard, scope)
    edit_row(
        store_path,
        result.action_id,
        scope_json='{"allowed_namespaces":["db"],"allowed_tables":["x"]}',
    )
    done = decide(pguard, verifier, result)
    assert_stale(pguard, orders, result, done, "hash", main_before=main_ids(orders))


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("sql_normalized", "DELETE FROM orders"),
        ("head_snapshot_id", 1),
        ("base_snapshot_id", 1),
        ("diff_digest", "forged"),
        ("policy_digest", "forged"),
        ("requester", "somebody-trusted"),
        ("table", "db.other"),
        ("engine", "spark"),
    ],
)
def test_any_tampering_with_a_stored_action_makes_it_stale(
    pguard, verifier, orders, scope, store_path, column, value
):
    result = queue(pguard, scope)
    edit_row(store_path, result.action_id, **{column: value})
    done = decide(pguard, verifier, result)
    assert done.outcome is Outcome.STALE and not done.published
    assert order_ids(orders) == list(range(1, ROWS + 1))
    if column != "table":  # a forged table name sends the clean-up to the wrong place
        assert not branches(orders)


def test_a_row_diff_that_differs_from_the_reviewed_one_makes_the_action_stale(
    pguard, verifier, orders, scope, monkeypatch
):
    from sqlguard.safety.guard import Guard as G

    result = queue(pguard, scope)
    real = G._row_diff

    def other(self, *args, **kwargs):
        return replace(real(self, *args, **kwargs), digest="0" * 64)

    monkeypatch.setattr(G, "_row_diff", other)
    done = decide(pguard, verifier, result)
    assert_stale(pguard, orders, result, done, "row diff differs", main_before=main_ids(orders))


def test_a_changed_invariant_set_makes_the_action_stale(
    make_guard, store_path, verifier, clock, orders, scope
):
    result = queue(make_guard(store=store_path, proof_verifier=verifier, clock=clock), scope)
    other = make_guard(
        store=store_path,
        proof_verifier=verifier,
        clock=clock,
        extra_invariants=[Invariant("extra", lambda ctx: None)],
    )
    done = decide(other, verifier, result)
    assert done.outcome is Outcome.STALE and "invariants changed" in " ".join(done.reasons)


def test_a_different_engine_cannot_decide_the_action(pguard, verifier, orders, scope, monkeypatch):
    result = queue(pguard, scope)
    monkeypatch.setattr(type(pguard.adapter), "engine", "other", raising=False)
    done = decide(pguard, verifier, result)
    assert done.outcome is Outcome.STALE and "queued on" in " ".join(done.reasons)


def test_an_invariant_that_fails_at_approval_makes_the_action_stale(
    make_guard, store_path, verifier, clock, orders, scope
):
    broken = {"now": False}
    flip = Invariant("flip", lambda ctx: "now failing" if broken["now"] else None)
    guard = make_guard(
        store=store_path, proof_verifier=verifier, clock=clock, extra_invariants=[flip]
    )
    result = queue(guard, scope)  # the invariant held when the action was rehearsed
    broken["now"] = True
    done = decide(guard, verifier, result)
    assert done.outcome is Outcome.STALE and "flip" in " ".join(done.reasons)
    assert order_ids(orders) == list(range(1, ROWS + 1)) and not branches(orders)


def test_main_moving_during_the_publish_is_stale_and_spends_the_proof(
    pguard, verifier, orders, scope, race_on_next_commit
):
    result = queue(pguard, scope)
    race_on_next_commit()
    done = decide(pguard, verifier, result)
    assert done.outcome is Outcome.STALE and not done.published
    assert pguard.store.get(result.action_id).state is A.STALE
    assert 999 in order_ids(orders) and len(order_ids(orders)) == ROWS + 1
    assert not branches(orders)


def test_a_failed_post_publish_check_rolls_back_and_the_action_fails(
    make_guard, store_path, verifier, clock, orders, scope
):
    late = Invariant("late", lambda ctx: "bad after publish", phases=frozenset({"post_publish"}))
    guard = make_guard(
        store=store_path, proof_verifier=verifier, clock=clock, extra_invariants=[late]
    )
    result = queue(guard, scope)
    done = decide(guard, verifier, result)
    assert done.outcome is Outcome.ROLLED_BACK and done.rolled_back and not done.published
    assert order_ids(orders) == list(range(1, ROWS + 1))
    assert guard.store.get(result.action_id).state is A.FAILED
    assert not branches(orders)


def test_a_writer_on_top_of_a_failed_publish_leaves_the_action_needing_attention(
    make_guard, store_path, verifier, clock, orders, scope
):
    def append_then_fail(ctx):
        orders.table("orders").append(
            pa.table(
                {"id": pa.array([888], pa.int64()), "customer": ["z"], "amount": pa.array([1.0])}
            )
        ) if 888 not in order_ids(orders) else None
        return "bad after publish"

    late = Invariant("late", append_then_fail, phases=frozenset({"post_publish"}))
    guard = make_guard(
        store=store_path, proof_verifier=verifier, clock=clock, extra_invariants=[late]
    )
    result = queue(guard, scope)
    done = decide(guard, verifier, result)
    assert done.outcome is Outcome.ROLLBACK_BLOCKED and done.needs_attention
    assert guard.store.get(result.action_id).state is A.NEEDS_ATTENTION
    assert 888 in order_ids(orders)  # the other writer's commit survives


# -- expiry ----------------------------------------------------------------------------------


def test_resolving_after_the_deadline_expires_the_action(pguard, verifier, orders, scope, clock):
    result = queue(pguard, scope)
    clock.advance(hours=25)
    done = decide(pguard, verifier, result)
    assert done.outcome is Outcome.EXPIRED and not done.published
    assert pguard.store.get(result.action_id).state is A.EXPIRED
    assert not branches(orders) and order_ids(orders) == list(range(1, ROWS + 1))
    clock.advance(hours=1)
    with pytest.raises(ActionNotPending):
        decide(pguard, verifier, result, nonce="n2")


def test_expire_pending_retires_only_the_late_ones_and_their_branches(
    pguard, verifier, orders, scope, clock, audit
):
    old = queue(pguard, scope)
    clock.advance(hours=20)
    young = queue(pguard, scope, "DELETE FROM orders WHERE id <= 16")
    clock.advance(hours=5)
    assert pguard.expire_pending() == [old.action_id]
    assert state_of(pguard, old) is A.EXPIRED and state_of(pguard, young) is A.PENDING
    assert branches(orders) == {pguard.store.get(young.action_id).branch}
    assert pguard.store.get(old.action_id).sql_normalized is None
    assert any(r.get("outcome") == "expired" for r in audit.read())
    assert pguard.expire_pending() == []


def test_the_pending_lifetime_is_configurable(make_guard, store_path, orders, scope):
    guard = make_guard(store=store_path, pending_ttl=timedelta(minutes=5))
    row = guard.store.get(queue(guard, scope).action_id)
    assert row.expires_at - row.created_at == timedelta(minutes=5)


# -- nothing sensitive is left behind --------------------------------------------------------


def literal_sql() -> str:
    return f"UPDATE orders SET customer = '{SECRET_LITERAL}' WHERE id <= 15"


@pytest.mark.parametrize("ending", ["approve", "reject", "stale", "expire"])
def test_a_finished_action_leaves_no_raw_sql_or_values_in_the_store(
    pguard, verifier, orders, scope, store_path, clock, ending
):
    result = queue(pguard, scope, literal_sql())
    assert SECRET_LITERAL.encode() in store_bytes(store_path)  # pending rows do hold it
    if ending == "approve":
        assert decide(pguard, verifier, result).outcome is Outcome.PUBLISHED
    elif ending == "reject":
        decide(pguard, verifier, result, "reject")
    elif ending == "stale":
        edit_row(store_path, result.action_id, requester="changed")
        assert decide(pguard, verifier, result).outcome is Outcome.STALE
    else:
        clock.advance(days=2)
        pguard.expire_pending()
    assert pguard.store.get(result.action_id).terminal
    blob = store_bytes(store_path)
    assert SECRET_LITERAL.encode() not in blob
    row = pguard.store.get(result.action_id)
    assert row.sql_normalized is None and SECRET_LITERAL not in row.sql_redacted
    assert SECRET_LITERAL not in str(row.to_dict())


def test_the_store_never_holds_rows_or_the_proof(pguard, verifier, orders, scope, store_path):
    orders.table("orders").overwrite(
        pa.table(
            {
                "id": pa.array(range(1, ROWS + 1), pa.int64()),
                "customer": [f"zzcustomer-{i}" for i in range(ROWS)],
                "amount": pa.array([float(i) for i in range(ROWS)]),
            }
        )
    )
    result = queue(pguard, scope)
    assert b"zzcustomer" not in store_bytes(store_path)
    request = ProofRequest(result.action_id, result.action_hash, "approve", APPROVER)
    proof = verifier.proof(request)
    decide(pguard, verifier, result)
    blob = store_bytes(store_path)
    assert proof.encode() not in blob and b"test-secret" not in blob
    assert b"zzcustomer" not in blob  # no row values, neither before nor after the decision


# -- reconcile: a crash between transitions --------------------------------------------------


def crash_during_publish(guard, *, after_commit: bool, monkeypatch):
    real = guard.adapter.publish

    def publish(branch, expected_head):
        if after_commit:
            real(branch, expected_head)
        raise Crash

    monkeypatch.setattr(guard.adapter, "publish", publish)


def fresh_guard(make_guard, store_path, verifier, clock, **kwargs):
    """What the next process sees: same catalog and store, no memory of the crash."""
    return make_guard(store=store_path, proof_verifier=verifier, clock=clock, **kwargs)


def test_an_engine_error_before_the_commit_fails_the_action(
    pguard, verifier, orders, scope, monkeypatch
):
    from sqlguard.safety.executor import ExecutionError

    result = queue(pguard, scope)

    def refuse(branch, expected_head):
        raise ExecutionError("the engine said no")

    monkeypatch.setattr(pguard.adapter, "publish", refuse)
    done = decide(pguard, verifier, result)
    assert done.outcome is Outcome.EXECUTION_FAILED and not done.published
    assert state_of(pguard, result) is A.FAILED and not branches(orders)
    assert order_ids(orders) == list(range(1, ROWS + 1))


def test_an_unexpected_error_leaves_the_action_in_publishing_for_reconcile(
    make_guard, pguard, verifier, orders, scope, store_path, clock, monkeypatch
):
    result = queue(pguard, scope)

    def explode(branch, expected_head):
        raise RuntimeError("who knows")

    monkeypatch.setattr(pguard.adapter, "publish", explode)
    done = decide(pguard, verifier, result)
    assert done.outcome is Outcome.ERROR and not done.published
    assert "reconcile" in " ".join(done.reasons)
    assert state_of(pguard, result) is A.PUBLISHING
    clock.advance(minutes=5)
    nxt = fresh_guard(make_guard, store_path, verifier, clock)
    assert [s.state for s in nxt.reconcile()] == [A.FAILED]


def test_a_crash_before_the_commit_is_settled_as_failed(
    make_guard, pguard, verifier, orders, scope, store_path, clock, monkeypatch
):
    result = queue(pguard, scope)
    crash_during_publish(pguard, after_commit=False, monkeypatch=monkeypatch)
    with pytest.raises(Crash):
        decide(pguard, verifier, result)
    assert state_of(pguard, result) is A.PUBLISHING
    assert order_ids(orders) == list(range(1, ROWS + 1))

    nxt = fresh_guard(make_guard, store_path, verifier, clock)
    assert nxt.reconcile() == []  # too young: another process may still be working on it
    clock.advance(minutes=3)
    settled = nxt.reconcile()
    assert [(s.action_id, s.state) for s in settled] == [(result.action_id, A.FAILED)]
    assert state_of(nxt, result) is A.FAILED and not branches(orders)
    assert order_ids(orders) == list(range(1, ROWS + 1))
    assert nxt.reconcile() == []


def test_a_crash_after_the_commit_is_settled_as_published(
    make_guard, pguard, verifier, orders, scope, store_path, clock, monkeypatch
):
    result = queue(pguard, scope)
    crash_during_publish(pguard, after_commit=True, monkeypatch=monkeypatch)
    with pytest.raises(Crash):
        decide(pguard, verifier, result)
    assert order_ids(orders) == list(range(16, ROWS + 1))

    nxt = fresh_guard(make_guard, store_path, verifier, clock)
    settled = nxt.reconcile(grace=timedelta(0))
    assert [(s.action_id, s.state) for s in settled] == [(result.action_id, A.PUBLISHED)]
    assert order_ids(orders) == list(range(16, ROWS + 1)) and not branches(orders)
    assert nxt.store.get(result.action_id).sql_normalized is None


def test_a_crash_after_the_commit_with_a_failing_check_is_rolled_back(
    make_guard, pguard, verifier, orders, scope, store_path, clock, monkeypatch
):
    late = Invariant("late", lambda ctx: "bad after publish", phases=frozenset({"post_publish"}))
    # queue with the late invariant already in place so the digests agree
    guard = fresh_guard(make_guard, store_path, verifier, clock, extra_invariants=[late])
    result = queue(guard, scope)
    crash_during_publish(guard, after_commit=True, monkeypatch=monkeypatch)
    with pytest.raises(Crash):
        decide(guard, verifier, result)
    assert order_ids(orders) == list(range(16, ROWS + 1))

    nxt = fresh_guard(make_guard, store_path, verifier, clock, extra_invariants=[late])
    settled = nxt.reconcile(grace=timedelta(0))
    assert [s.state for s in settled] == [A.FAILED] and "rolled back" in settled[0].reason
    assert order_ids(orders) == list(range(1, ROWS + 1))


def test_a_crash_between_the_commit_and_the_final_transition_is_settled(
    make_guard, pguard, verifier, orders, scope, store_path, clock, monkeypatch
):
    result = queue(pguard, scope)

    def die(*args, **kwargs):
        raise Crash

    monkeypatch.setattr(type(pguard), "_finalize", die)
    with pytest.raises(Crash):
        decide(pguard, verifier, result)
    assert state_of(pguard, result) is A.PUBLISHING
    assert order_ids(orders) == list(range(16, ROWS + 1))
    monkeypatch.undo()

    nxt = fresh_guard(make_guard, store_path, verifier, clock)
    assert [s.state for s in nxt.reconcile(grace=timedelta(0))] == [A.PUBLISHED]


def test_another_writer_on_top_of_a_crashed_publish_is_verified_not_rolled_back(
    make_guard, pguard, verifier, orders, scope, store_path, clock, monkeypatch
):
    result = queue(pguard, scope)
    crash_during_publish(pguard, after_commit=True, monkeypatch=monkeypatch)
    with pytest.raises(Crash):
        decide(pguard, verifier, result)
    orders.table("orders").append(one_row(600))
    nxt = fresh_guard(make_guard, store_path, verifier, clock)
    assert [s.state for s in nxt.reconcile(grace=timedelta(0))] == [A.PUBLISHED]
    assert 600 in order_ids(orders)


def test_a_failing_check_with_another_writer_on_top_needs_attention_and_is_not_undone(
    make_guard, verifier, orders, scope, store_path, clock, monkeypatch
):
    late = Invariant("late", lambda ctx: "bad after publish", phases=frozenset({"post_publish"}))
    guard = fresh_guard(make_guard, store_path, verifier, clock, extra_invariants=[late])
    result = queue(guard, scope)
    crash_during_publish(guard, after_commit=True, monkeypatch=monkeypatch)
    with pytest.raises(Crash):
        decide(guard, verifier, result)
    orders.table("orders").append(one_row(600))
    nxt = fresh_guard(make_guard, store_path, verifier, clock, extra_invariants=[late])
    settled = nxt.reconcile(grace=timedelta(0))
    assert [s.state for s in settled] == [A.NEEDS_ATTENTION]
    assert order_ids(orders) == [*range(16, ROWS + 1), 600]  # nothing was undone


def test_main_somewhere_else_after_a_crash_needs_attention(
    make_guard, pguard, verifier, orders, scope, store_path, clock, monkeypatch
):
    result = queue(pguard, scope)
    crash_during_publish(pguard, after_commit=False, monkeypatch=monkeypatch)
    with pytest.raises(Crash):
        decide(pguard, verifier, result)
    orders.table("orders").append(one_row(700))
    nxt = fresh_guard(make_guard, store_path, verifier, clock)
    settled = nxt.reconcile(grace=timedelta(0))
    assert [s.state for s in settled] == [A.NEEDS_ATTENTION]
    assert 700 in order_ids(orders)


def test_a_real_process_killed_right_after_the_commit_is_settled(
    make_guard, orders, scope, store_path, verifier, clock, tmp_path, audit_path
):
    guard = make_guard(store=store_path, proof_verifier=AcceptAnything(), clock=clock)
    result = queue(guard, scope)
    code = (
        "import os, sys\n"
        "from sqlguard.core.catalog import LakehouseCatalog\n"
        "from sqlguard.safety import Guard\n"
        "warehouse, store, audit, action_id = sys.argv[1:]\n"
        "class Accept:\n"
        "    def verify(self, request, proof): return True\n"
        "guard = Guard(LakehouseCatalog.local(warehouse), audit_path=audit, store=store,\n"
        "              proof_verifier=Accept())\n"
        "real = guard.adapter.publish\n"
        "def publish(branch, head):\n"
        "    real(branch, head)\n"
        "    os._exit(9)\n"
        "guard.adapter.publish = publish\n"
        "guard.resolve(action_id, 'approve', 'bob', 'proof-from-subprocess')\n"
    )
    warehouse = tmp_path / "warehouse"
    run = subprocess.run(
        [
            sys.executable,
            "-c",
            code,
            str(warehouse),
            str(store_path),
            str(audit_path),
            result.action_id,
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert run.returncode == 9, run.stderr
    assert state_of(guard, result) is A.PUBLISHING
    assert order_ids(orders) == list(range(16, ROWS + 1))

    clock.advance(minutes=5)
    settled = guard.reconcile()
    assert [(s.action_id, s.state) for s in settled] == [(result.action_id, A.PUBLISHED)]
    assert not branches(orders)


# -- maintenance through the pending path ----------------------------------------------------

FAR = datetime.now(UTC) + timedelta(days=60)  # snapshots written by these tests look two months old


@pytest.fixture
def history(orders):
    for i in (1, 2, 3):
        orders.table("orders").append(one_row(1000 + i))
    return [s.snapshot_id for s in orders.table("orders").snapshots()]


@pytest.fixture
def mguard(make_guard, store_path, verifier):
    return make_guard(store=store_path, proof_verifier=verifier, clock=lambda: FAR)


def expire_action(**kwargs) -> ExpireSnapshots:
    return ExpireSnapshots("orders", older_than=FAR - timedelta(days=10), **kwargs)


def test_expire_snapshots_waits_in_the_store_with_its_plan(mguard, orders, history, scope):
    result = mguard.submit(expire_action(), scope)
    assert result.outcome is Outcome.AWAITING_APPROVAL and result.action_id and not result.executed
    row = mguard.store.get(result.action_id)
    assert row.kind == "expire_snapshots" and row.branch is None and row.sql_normalized is None
    plan = row.maintenance_data["plan"]
    assert plan["expired_snapshot_ids"] == history[:-1]
    assert row.impact_data["plan"]["remaining_snapshots"] == 1
    assert "orphan_files" not in row.impact_data["plan"]
    assert len(orders.table("orders").snapshots()) == len(history)


def test_approving_expire_replans_and_applies(mguard, verifier, orders, history, scope):
    result = mguard.submit(expire_action(), scope)
    done = decide(mguard, verifier, result)
    assert done.outcome is Outcome.MAINTENANCE_DONE and done.ok
    assert [s.snapshot_id for s in orders.table("orders").snapshots()] == [history[-1]]
    row = mguard.store.get(result.action_id)
    assert row.state is A.PUBLISHED and row.maintenance_json is None


def test_expire_is_stale_when_a_re_plan_would_remove_something_else(
    mguard, verifier, orders, history, scope
):
    result = mguard.submit(expire_action(), scope)
    orders.table("orders").append(one_row(2000))  # the old head would now be expired as well
    done = decide(mguard, verifier, result)
    assert done.outcome is Outcome.STALE and "plan differs" in " ".join(done.reasons)
    assert len(orders.table("orders").snapshots()) == len(history) + 1


def test_a_rollback_waits_then_moves_main_when_nothing_changed(
    mguard, verifier, orders, history, scope
):
    result = mguard.submit(RollbackToSnapshot("orders", snapshot_id=history[0]), scope)
    assert result.outcome is Outcome.AWAITING_APPROVAL and result.action_id
    row = mguard.store.get(result.action_id)
    assert (row.base_snapshot_id, row.head_snapshot_id) == (history[-1], history[0])
    done = decide(mguard, verifier, result)
    assert done.outcome is Outcome.MAINTENANCE_DONE and done.published
    assert order_ids(orders) == list(range(1, ROWS + 1))


def test_a_rollback_is_stale_when_main_moved(mguard, verifier, orders, history, scope):
    result = mguard.submit(RollbackToSnapshot("orders", snapshot_id=history[0]), scope)
    orders.table("orders").append(one_row(3000))
    done = decide(mguard, verifier, result)
    assert done.outcome is Outcome.STALE and 3000 in order_ids(orders)


def test_a_maintenance_policy_change_makes_it_stale(
    make_guard, store_path, verifier, orders, history, scope
):
    result = make_guard(store=store_path, proof_verifier=verifier, clock=lambda: FAR).submit(
        expire_action(), scope
    )
    stricter = make_guard(
        Policy.from_dict({"retention": {"default": {"min_snapshot_age_days": 365}}}),
        store=store_path,
        proof_verifier=verifier,
        clock=lambda: FAR,
    )
    done = decide(stricter, verifier, result)
    assert done.outcome is Outcome.STALE
    assert len(orders.table("orders").snapshots()) == len(history)


def test_a_retention_rule_that_starts_to_deny_it_makes_it_stale_even_for_one_policy(
    make_guard, store_path, verifier, orders, history, scope
):
    """Same policy, later time: the cutoff the agent chose is now too recent for the rule."""
    clock = Clock()
    clock.now = FAR
    guard = make_guard(store=store_path, proof_verifier=verifier, clock=clock)
    result = guard.submit(ExpireSnapshots("orders", older_than=FAR - timedelta(days=8)), scope)
    assert result.outcome is Outcome.AWAITING_APPROVAL
    clock.now = FAR - timedelta(days=5)  # an earlier "now": the cutoff is now only 3 days old
    done = decide(guard, verifier, result)
    reasons = " ".join(done.reasons)
    assert done.outcome is Outcome.STALE and ("denies" in reasons or "retention" in reasons)
    assert len(orders.table("orders").snapshots()) == len(history)


def test_maintenance_interrupted_mid_apply_needs_attention(
    make_guard, mguard, verifier, orders, history, scope, store_path, monkeypatch
):
    result = mguard.submit(expire_action(), scope)

    def die(*args, **kwargs):
        raise Crash

    monkeypatch.setattr(mguard.adapter, "apply_maintenance", die)
    with pytest.raises(Crash):
        decide(mguard, verifier, result)
    nxt = make_guard(store=store_path, proof_verifier=verifier, clock=lambda: FAR)
    settled = nxt.reconcile(grace=timedelta(0))
    assert [s.state for s in settled] == [A.NEEDS_ATTENTION]


def test_a_pointer_move_is_judged_by_where_main_is_after_a_crash(
    make_guard, mguard, verifier, orders, history, scope, store_path, monkeypatch
):
    result = mguard.submit(RollbackToSnapshot("orders", snapshot_id=history[0]), scope)
    real = mguard.adapter.apply_maintenance

    def apply_then_die(*args, **kwargs):
        real(*args, **kwargs)
        raise Crash

    monkeypatch.setattr(mguard.adapter, "apply_maintenance", apply_then_die)
    with pytest.raises(Crash):
        decide(mguard, verifier, result)
    nxt = make_guard(store=store_path, proof_verifier=verifier, clock=lambda: FAR)
    assert [s.state for s in nxt.reconcile(grace=timedelta(0))] == [A.PUBLISHED]


def test_pointer_move_crash_before_the_move_is_failed(
    make_guard, mguard, verifier, orders, history, scope, store_path, monkeypatch
):
    result = mguard.submit(RollbackToSnapshot("orders", snapshot_id=history[0]), scope)

    def die(*args, **kwargs):
        raise Crash

    monkeypatch.setattr(mguard.adapter, "apply_maintenance", die)
    with pytest.raises(Crash):
        decide(mguard, verifier, result)
    nxt = make_guard(store=store_path, proof_verifier=verifier, clock=lambda: FAR)
    assert [s.state for s in nxt.reconcile(grace=timedelta(0))] == [A.FAILED]
    assert order_ids(orders) == sorted([*range(1, ROWS + 1), 1001, 1002, 1003])


# -- for the review UI -----------------------------------------------------------------------


def test_the_diff_is_recomputed_from_the_snapshots_and_matches_the_reviewed_digest(
    pguard, orders, scope
):
    result = queue(pguard, scope)
    row = pguard.store.get(result.action_id)
    diff = pguard.diff_for(result.action_id)
    assert diff.digest == row.diff_digest
    assert (diff.rows_deleted, diff.mode) == (15, "unkeyed")
    assert len(diff.rows) == 15 and diff.pushdown is not None  # id <= 15 is pushed into the scans
    assert diff.rows[0].before["id"] in range(1, 16)


def test_the_diff_uses_the_tables_identifier_fields_as_key(pguard, orders, scope):
    with orders.table("orders").update_schema(allow_incompatible_changes=True) as update:
        update.update_column("id", required=True)
        update.set_identifier_fields("id")
    result = queue(pguard, scope, "UPDATE orders SET amount = amount + 100 WHERE id <= 15")
    diff = pguard.diff_for(result.action_id)
    assert (diff.mode, diff.key, diff.key_source) == ("keyed", ("id",), "identifier")
    assert (
        diff.rows_modified == 15 and diff.digest == pguard.store.get(result.action_id).diff_digest
    )
    assert decide(pguard, HmacVerifier(), result).outcome is Outcome.PUBLISHED
