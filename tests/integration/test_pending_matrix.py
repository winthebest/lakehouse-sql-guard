"""Pending actions on real catalogs: queue, approve, reject, stale, reconcile, expire.

Each test runs once per catalog (Lakekeeper, the REST fixture, Polaris) with the PyIceberg-backed
adapter; the last block runs the same life cycle on Lakekeeper with Trino reading and PyIceberg
writing, and checks how the Guard records a Trino maintenance that ran and then left the table off
plan. Approvals use an HMAC proof like the unit tests; a "crash" is a `BaseException` raised at the
chosen point of the publish, after which a fresh Guard on the same store reconciles.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pyarrow as pa
import pytest

from sqlguard.core.catalog import LakehouseCatalog
from sqlguard.safety import (
    ActionState,
    Guard,
    Policy,
    ProofRequest,
    RollbackToSnapshot,
    SqlAction,
    TaskScope,
)
from sqlguard.safety.executor import DuckDBIcebergAdapter
from sqlguard.safety.executor.composite import CompositeAdapter
from sqlguard.safety.executor.trino_iceberg import TrinoIcebergAdapter
from sqlguard.safety.guard import Outcome
from tests.integration.conftest import ROWS, main_id, order_ids, setting

RISKY = "DELETE FROM orders WHERE id <= 15"  # 75% of the rows: the rehearsal says ask
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
    """Stand-in for the real verifier: an HMAC over the exact request plus a nonce."""

    secret = b"integration-demo-secret"

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
        return hmac.compare_digest(self.proof(request, proof.partition(".")[0]), proof)


class Crash(BaseException):
    """The process dying: nothing in the Guard may catch it."""


@pytest.fixture(autouse=True)
def linux_identity(monkeypatch: pytest.MonkeyPatch):
    """A fixed boot id and PID namespace, so a dead publisher can be proven the same everywhere."""
    import sqlguard.safety.guard as guard_module

    monkeypatch.setattr(guard_module, "_boot_id", lambda: "boot-1")
    monkeypatch.setattr(guard_module, "_pid_namespace", lambda: "pid:[1]")


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def verifier() -> HmacVerifier:
    return HmacVerifier()


@pytest.fixture
def scope(lake: LakehouseCatalog) -> TaskScope:
    return TaskScope.of(namespaces=[lake.namespace])


@pytest.fixture
def store_path(tmp_path: Path) -> Path:
    return tmp_path / "state" / "actions.db"


@pytest.fixture
def make_guard(lake: LakehouseCatalog, tmp_path: Path, store_path: Path, verifier, clock):
    def build(adapter=None, **kwargs) -> Guard:
        return Guard(
            lake,
            Policy(),
            audit_path=tmp_path / "audit.jsonl",
            adapter=adapter or DuckDBIcebergAdapter(lake),
            store=store_path,
            proof_verifier=verifier,
            clock=clock,
            **kwargs,
        )

    return build


@pytest.fixture
def pguard(make_guard) -> Guard:
    return make_guard()


def queue(guard: Guard, scope: TaskScope, sql: str = RISKY):
    result = guard.submit(SqlAction(sql), scope)
    assert result.outcome is Outcome.AWAITING_APPROVAL and result.action_id, result.reasons
    return result


def decide(guard: Guard, verifier: HmacVerifier, result, decision: str = "approve"):
    request = ProofRequest(result.action_id, result.action_hash, decision, APPROVER)
    return guard.resolve(result.action_id, decision, APPROVER, verifier.proof(request))


def branches(lake: LakehouseCatalog) -> set[str]:
    return set(lake.refs("orders")) - {"main"}


def one_row(order_id: int) -> pa.Table:
    return pa.table(
        {"id": pa.array([order_id], pa.int64()), "customer": ["w"], "amount": pa.array([1.0])}
    )


def settle(guard: Guard, clock: Clock):
    """Let the dead publisher's lease run out, then reconcile."""
    clock.advance(minutes=6)
    return guard.reconcile()


def crash_during_publish(guard: Guard, *, after_commit: bool, monkeypatch: pytest.MonkeyPatch):
    real = guard.adapter.publish

    def publish(branch, expected_head):
        if after_commit:
            real(branch, expected_head)
        raise Crash

    monkeypatch.setattr(guard.adapter, "publish", publish)


# -- queue, approve, reject ----------------------------------------------------------------------


def test_submit_queues_a_rehearsed_change_and_keeps_its_branch(
    pguard: Guard, lake: LakehouseCatalog, scope: TaskScope
):
    base = main_id(lake)
    result = queue(pguard, scope)
    row = pguard.store.get(result.action_id)

    assert row.state is A.PENDING and row.base_snapshot_id == base
    assert main_id(lake) == base and order_ids(lake) == list(range(1, ROWS + 1))
    assert branches(lake) == {row.branch}
    assert lake.refs("orders")[row.branch] == row.head_snapshot_id != base

    # the stored row diff and the measured impact agree, and the diff is reproducible from snapshots
    stored = row.impact_data
    impact = stored["impact"]["tables"][0]
    assert stored["row_diff"]["rows_deleted"] == impact["rows_deleted"] == 15
    assert stored["row_diff"]["rows_added"] == impact["rows_added"] == 0
    diff = pguard.diff_for(result.action_id)
    assert diff.rows_deleted == 15 and diff.digest == row.diff_digest


def test_approving_publishes_exactly_the_rehearsed_head(
    pguard: Guard, verifier: HmacVerifier, lake: LakehouseCatalog, scope: TaskScope
):
    result = queue(pguard, scope)
    head = pguard.store.get(result.action_id).head_snapshot_id

    done = decide(pguard, verifier, result)
    assert done.outcome is Outcome.PUBLISHED and done.published
    assert main_id(lake) == head
    assert order_ids(lake) == list(range(16, ROWS + 1))
    assert not branches(lake)
    row = pguard.store.get(result.action_id)
    assert row.state is A.PUBLISHED and row.approver == APPROVER and row.sql_normalized is None


def test_rejecting_drops_the_branch_and_leaves_main_alone(
    pguard: Guard, verifier: HmacVerifier, lake: LakehouseCatalog, scope: TaskScope
):
    base = main_id(lake)
    result = queue(pguard, scope)
    assert len(branches(lake)) == 1

    done = decide(pguard, verifier, result, "reject")
    assert done.outcome is Outcome.REJECTED and not done.published
    assert main_id(lake) == base and not branches(lake)
    assert pguard.store.get(result.action_id).state is A.REJECTED


def test_main_written_between_queue_and_approve_makes_it_stale(
    pguard: Guard, verifier: HmacVerifier, lake: LakehouseCatalog, scope: TaskScope
):
    result = queue(pguard, scope)
    lake.table("orders").append(one_row(500))
    moved = main_id(lake)

    done = decide(pguard, verifier, result)
    assert done.outcome is Outcome.STALE and not done.published
    assert main_id(lake) == moved and 500 in order_ids(lake)  # their commit is untouched
    assert not branches(lake)  # the rehearsal branch is gone
    assert pguard.store.get(result.action_id).state is A.STALE


def test_the_branch_written_after_the_review_makes_it_stale(
    pguard: Guard, verifier: HmacVerifier, lake: LakehouseCatalog, scope: TaskScope
):
    result = queue(pguard, scope)
    row = pguard.store.get(result.action_id)
    lake.table("orders").append(one_row(501), branch=row.branch)  # not what was reviewed

    done = decide(pguard, verifier, result)
    assert done.outcome is Outcome.STALE
    assert order_ids(lake) == list(range(1, ROWS + 1)) and not branches(lake)


def test_a_proof_is_single_use(pguard: Guard, verifier: HmacVerifier, scope: TaskScope):
    from sqlguard.safety import ActionNotPending, ProofRejected

    result = queue(pguard, scope)
    decide(pguard, verifier, result)
    with pytest.raises((ProofRejected, ActionNotPending)):
        decide(pguard, verifier, result)


# -- expiry --------------------------------------------------------------------------------------


def test_expire_pending_drops_the_branch_of_a_late_action(
    pguard: Guard, lake: LakehouseCatalog, scope: TaskScope, clock: Clock
):
    base = main_id(lake)
    result = queue(pguard, scope)
    assert len(branches(lake)) == 1
    clock.advance(hours=25)

    assert pguard.expire_pending() == [result.action_id]
    assert pguard.store.get(result.action_id).state is A.EXPIRED
    assert not branches(lake) and main_id(lake) == base


def test_resolving_after_the_deadline_expires_it(
    pguard: Guard, verifier: HmacVerifier, lake: LakehouseCatalog, scope: TaskScope, clock: Clock
):
    result = queue(pguard, scope)
    clock.advance(hours=25)
    done = decide(pguard, verifier, result)
    assert done.outcome is Outcome.EXPIRED and not done.published
    assert not branches(lake) and order_ids(lake) == list(range(1, ROWS + 1))


# -- reconcile after a crash ---------------------------------------------------------------------


def test_a_crash_before_the_commit_is_settled_as_failed(
    make_guard, pguard, verifier, lake, scope, clock, monkeypatch
):
    result = queue(pguard, scope)
    crash_during_publish(pguard, after_commit=False, monkeypatch=monkeypatch)
    with pytest.raises(Crash):
        decide(pguard, verifier, result)
    assert pguard.store.get(result.action_id).state is A.PUBLISHING
    assert order_ids(lake) == list(range(1, ROWS + 1))

    nxt = make_guard()
    assert nxt.reconcile() == []  # the publisher's lease is still running
    settled = settle(nxt, clock)
    assert [(s.action_id, s.state) for s in settled] == [(result.action_id, A.FAILED)]
    assert not branches(lake) and order_ids(lake) == list(range(1, ROWS + 1))
    assert nxt.reconcile() == []


def test_a_crash_after_the_commit_is_settled_as_published(
    make_guard, pguard, verifier, lake, scope, clock, monkeypatch
):
    result = queue(pguard, scope)
    head = pguard.store.get(result.action_id).head_snapshot_id
    crash_during_publish(pguard, after_commit=True, monkeypatch=monkeypatch)
    with pytest.raises(Crash):
        decide(pguard, verifier, result)
    assert main_id(lake) == head  # the commit landed before the process "died"

    settled = settle(make_guard(), clock)
    assert [(s.action_id, s.state) for s in settled] == [(result.action_id, A.PUBLISHED)]
    assert order_ids(lake) == list(range(16, ROWS + 1)) and not branches(lake)
    assert main_id(lake) == head


def test_a_crash_between_the_commit_and_the_final_record_is_settled(
    make_guard, pguard, verifier, lake, scope, clock, monkeypatch
):
    result = queue(pguard, scope)

    def die(*args, **kwargs):
        raise Crash

    monkeypatch.setattr(type(pguard), "_finalize", die)
    with pytest.raises(Crash):
        decide(pguard, verifier, result)
    monkeypatch.undo()
    assert pguard.store.get(result.action_id).state is A.PUBLISHING
    assert order_ids(lake) == list(range(16, ROWS + 1))

    assert [s.state for s in settle(make_guard(), clock)] == [A.PUBLISHED]


def test_another_writer_on_top_of_a_crashed_publish_is_not_undone(
    make_guard, pguard, verifier, lake, scope, clock, monkeypatch
):
    result = queue(pguard, scope)
    crash_during_publish(pguard, after_commit=True, monkeypatch=monkeypatch)
    with pytest.raises(Crash):
        decide(pguard, verifier, result)
    lake.table("orders").append(one_row(600))

    assert [s.state for s in settle(make_guard(), clock)] == [A.PUBLISHED]
    assert 600 in order_ids(lake)


def test_main_somewhere_else_after_a_crash_needs_attention(
    make_guard, pguard, verifier, lake, scope, clock, monkeypatch
):
    result = queue(pguard, scope)
    crash_during_publish(pguard, after_commit=False, monkeypatch=monkeypatch)
    with pytest.raises(Crash):
        decide(pguard, verifier, result)
    lake.table("orders").append(one_row(700))

    assert [s.state for s in settle(make_guard(), clock)] == [A.NEEDS_ATTENTION]
    assert 700 in order_ids(lake)


# -- Trino reads, PyIceberg writes (Lakekeeper) --------------------------------------------------


@pytest.fixture
def lk(lakekeeper_lake: LakehouseCatalog) -> LakehouseCatalog:
    return lakekeeper_lake


@pytest.fixture
def trino(lk: LakehouseCatalog) -> Iterator[TrinoIcebergAdapter]:
    yield TrinoIcebergAdapter.connect(
        lk,
        host=setting("TRINO_HOST"),
        port=int(setting("TRINO_PORT")),
        trino_catalog=setting("TRINO_CATALOG"),
    )


@pytest.fixture
def composite_guard(lk, trino, tmp_path, store_path, verifier, clock) -> Guard:
    return Guard(
        lk,
        Policy(),
        audit_path=tmp_path / "audit.jsonl",
        adapter=CompositeAdapter(read=trino, write=DuckDBIcebergAdapter(lk)),
        store=store_path,
        proof_verifier=verifier,
        clock=clock,
    )


def test_composite_queue_then_approve_measures_with_trino_and_publishes_that_head(
    composite_guard: Guard, verifier: HmacVerifier, lk: LakehouseCatalog, trino
):
    scope = TaskScope.of(namespaces=[lk.namespace])
    result = queue(composite_guard, scope)
    row = composite_guard.store.get(result.action_id)
    # Trino sees the branch the write engine made, and the head it measured is the stored one
    assert trino.refs("orders")[row.branch] == row.head_snapshot_id
    assert row.impact_data["row_diff"]["rows_deleted"] == 15
    assert composite_guard.diff_for(result.action_id).digest == row.diff_digest

    done = decide(composite_guard, verifier, result)
    assert done.outcome is Outcome.PUBLISHED
    assert main_id(lk) == row.head_snapshot_id
    assert sorted(trino.read("orders").column("id").to_pylist()) == list(range(16, ROWS + 1))
    assert not branches(lk)


def test_composite_stale_and_reject(composite_guard: Guard, verifier: HmacVerifier, lk):
    scope = TaskScope.of(namespaces=[lk.namespace])
    first = queue(composite_guard, scope)
    lk.table("orders").append(one_row(500))
    assert decide(composite_guard, verifier, first).outcome is Outcome.STALE
    assert not branches(lk) and 500 in order_ids(lk)

    second = queue(composite_guard, scope, "DELETE FROM orders WHERE id <= 16")
    assert len(branches(lk)) == 1
    done = decide(composite_guard, verifier, second, "reject")
    assert done.outcome is Outcome.REJECTED and not branches(lk)


def test_composite_crash_after_the_commit_is_settled_as_published(
    composite_guard: Guard,
    verifier: HmacVerifier,
    lk: LakehouseCatalog,
    tmp_path,
    store_path,
    clock,
    trino,
    monkeypatch,
):
    scope = TaskScope.of(namespaces=[lk.namespace])
    result = queue(composite_guard, scope)
    crash_during_publish(composite_guard, after_commit=True, monkeypatch=monkeypatch)
    with pytest.raises(Crash):
        decide(composite_guard, verifier, result)
    monkeypatch.undo()

    nxt = Guard(
        lk,
        Policy(),
        audit_path=tmp_path / "audit.jsonl",
        adapter=CompositeAdapter(read=trino, write=DuckDBIcebergAdapter(lk)),
        store=store_path,
        proof_verifier=verifier,
        clock=clock,
    )
    assert [s.state for s in settle(nxt, clock)] == [A.PUBLISHED]
    assert order_ids(lk) == list(range(16, ROWS + 1))


# -- the Guard's record of a Trino maintenance that ran and left the table off plan ---------------


def test_a_trino_rollback_that_displaces_a_ref_is_recorded_as_needing_attention(
    lk: LakehouseCatalog, trino: TrinoIcebergAdapter, tmp_path, monkeypatch
):
    base = main_id(lk)
    lk.table("orders").append(one_row(100))
    lk.table("orders").manage_snapshots().create_branch(main_id(lk), "feature").commit()

    original = trino._run

    def run_then_lose_a_branch(text: str) -> None:
        original(text)  # the rollback really happens on Trino...
        if "rollback_to_snapshot" in text:  # ...and then a branch disappears (another writer)
            lk.table("orders").manage_snapshots().remove_branch("feature").commit()

    monkeypatch.setattr(trino, "_run", run_then_lose_a_branch)
    guard = Guard(lk, Policy(), audit_path=tmp_path / "audit.jsonl", adapter=trino)
    scope = TaskScope.of(namespaces=[lk.namespace])

    result = guard.submit(RollbackToSnapshot("orders", snapshot_id=base), scope, lambda r: True)
    assert result.outcome is Outcome.ROLLBACK_BLOCKED
    assert result.needs_attention and result.executed and not result.ok
    assert main_id(lk) == base  # it did run
    assert any("a person must look" in reason for reason in result.reasons)
    records = [r for r in guard.audit.read() if r.get("phase") == "completed"]
    assert records[-1]["needs_attention"] is True and records[-1]["executed"] is True
