"""The guard behaviour the approval UI relies on when it runs housekeeping every minute:
honest reconcile reasons, evidence kept for people, bounded sweeps, fair queue limits and a
lease that survives a long check."""

from __future__ import annotations

import threading
import time
from datetime import timedelta

import pyarrow as pa
import pytest

from sqlguard.safety import ActionState, ProofRequest, SqlAction
from sqlguard.safety.guard import Outcome
from tests.approvals.conftest import Crash, branches, edit_row, queue

A = ActionState


def decide(guard, proofs, result, decision="approve", label="alice"):
    request = ProofRequest(result.action_id, result.action_hash, decision, label)
    return guard.resolve(result.action_id, decision, label, proofs.mint(request))


def crash_a_publish(guard, proofs, result, monkeypatch):
    def publish(branch, expected_head):
        raise Crash

    monkeypatch.setattr(guard.adapter, "publish", publish)
    with pytest.raises(Crash):
        decide(guard, proofs, result)
    assert guard.store.get(result.action_id).state is A.PUBLISHING


# -- the reason for an owner that cannot be proven gone ----------------------------------------


def test_an_unprovable_owner_is_reported_as_unprovable_not_as_another_host(
    pguard, proofs, orders, scope, clock, monkeypatch, macos_identity
):
    result = queue(pguard, scope)
    crash_a_publish(pguard, proofs, result, monkeypatch)
    clock.advance(minutes=6)
    [settled] = pguard.reconcile()
    assert settled.state is A.NEEDS_ATTENTION
    assert "cannot be proven gone" in settled.reason and "another host" not in settled.reason
    assert "may still land" in settled.reason


def test_an_owner_on_another_machine_gets_the_same_honest_reason(
    pguard, proofs, orders, scope, clock, monkeypatch, store_path, linux_identity
):
    result = queue(pguard, scope)
    crash_a_publish(pguard, proofs, result, monkeypatch)
    edit_row(store_path, result.action_id, owner="some-other-host|boot-1|pid:[1]|77|tok")
    clock.advance(minutes=6)
    [settled] = pguard.reconcile()
    assert "cannot be proven gone" in settled.reason


# -- evidence a person was asked to look at is not swept ---------------------------------------


def test_the_branch_of_a_needs_attention_action_is_never_swept(
    pguard, proofs, orders, scope, clock, monkeypatch, macos_identity
):
    result = queue(pguard, scope)
    crash_a_publish(pguard, proofs, result, monkeypatch)
    clock.advance(minutes=6)
    assert [s.state for s in pguard.reconcile()] == [A.NEEDS_ATTENTION]
    kept = pguard.store.get(result.action_id).branch
    assert kept in pguard.store.live_branches()

    clock.advance(days=30)
    assert pguard.expire_pending() == [] and pguard.sweep_orphan_branches(clock.now) == []
    assert branches(orders) == {kept}  # still there for the person to inspect


def test_a_branch_nobody_owns_is_still_swept(pguard, orders, scope, clock):
    adapter = pguard.adapter
    orphan = adapter.create_branch("orders")
    adapter.apply_sql(orphan, "DELETE FROM orders WHERE id <= 12")
    clock.advance(days=3)
    assert pguard.sweep_orphan_branches() == [f"db.orders:{orphan.name}"]


# -- sweeping costs a round trip per table, so it is bounded -----------------------------------


@pytest.fixture
def loaded_tables(pguard, monkeypatch):
    """The tables housekeeping loads from the catalog, in order."""
    loaded: list[str] = []
    catalog = pguard.catalog.catalog
    real = catalog.load_table

    def spy(identifier, *args, **kwargs):
        loaded.append(identifier if isinstance(identifier, str) else ".".join(identifier))
        return real(identifier, *args, **kwargs)

    monkeypatch.setattr(catalog, "load_table", spy)
    return loaded


def add_tables(orders, count: int = 4) -> list[str]:
    names = [f"extra{n}" for n in range(count)]
    for name in names:
        orders.create_table(name, pa.table({"id": pa.array([1, 2, 3], pa.int64())}))
    return names


def test_housekeeping_looks_at_every_table_only_once_in_a_while(
    pguard, orders, scope, clock, loaded_tables
):
    names = add_tables(orders)
    queue(pguard, scope)  # the only table with an action is orders
    loaded_tables.clear()

    pguard.expire_pending()  # the first run is a full sweep
    assert {f"db.{n}" for n in names} | {"db.orders"} <= set(loaded_tables)

    loaded_tables.clear()
    clock.advance(minutes=10)
    pguard.expire_pending()
    pguard.reconcile()
    assert set(loaded_tables) == {"db.orders"}  # between full sweeps: tables with actions only

    loaded_tables.clear()
    clock.advance(hours=1)
    pguard.expire_pending()
    assert {f"db.{n}" for n in names} <= set(loaded_tables)  # and a full one again


def test_a_stranded_branch_on_an_unrelated_table_waits_for_the_full_sweep(
    make_guard, store_path, proofs, orders, scope, clock
):
    guard = make_guard(
        store=store_path, proof_verifier=proofs, clock=clock, full_sweep_every=timedelta(days=30)
    )
    add_tables(orders, 1)
    queue(guard, scope)
    guard.expire_pending()  # the first run: full
    stranded_known = guard.adapter.create_branch("orders")
    guard.adapter.apply_sql(stranded_known, "DELETE FROM orders WHERE id <= 12")
    stranded_other = guard.adapter.create_branch("extra0")
    guard.adapter.apply_sql(stranded_other, "DELETE FROM extra0 WHERE id <= 2")
    clock.advance(days=3)
    guard.expire_pending()  # a table with an action is swept, the unrelated one is not (yet)
    assert stranded_known.name not in branches(orders)
    assert stranded_other.name in branches(orders, "extra0")
    assert guard.sweep_orphan_branches() == [f"db.extra0:{stranded_other.name}"]


def test_sweeping_named_tables_ignores_the_rest(pguard, orders, scope, clock, loaded_tables):
    add_tables(orders, 2)
    loaded_tables.clear()
    assert pguard.sweep_orphan_branches(tables=["db.extra1"]) == []
    assert loaded_tables == ["db.extra1"]


def test_the_store_knows_which_tables_have_had_actions(pguard, scope):
    assert pguard.store.action_tables() == set()
    queue(pguard, scope)
    assert pguard.store.action_tables() == {"db.orders"}
    assert pguard.store.action_tables(catalog_name="elsewhere") == set()


# -- one requester cannot fill the queue -------------------------------------------------------


def test_a_requester_has_its_own_queue_limit(make_guard, store_path, proofs, orders, scope):
    guard = make_guard(
        store=store_path, proof_verifier=proofs, max_pending=10, max_pending_per_requester=2
    )
    queue(guard, scope, requester="noisy")
    queue(guard, scope, "DELETE FROM orders WHERE id <= 16", requester="noisy")
    refused = guard.submit(SqlAction("DELETE FROM orders WHERE id <= 17"), scope, requester="noisy")
    assert refused.outcome is Outcome.DENIED and "from noisy" in " ".join(refused.reasons)
    assert len(branches(orders)) == 2  # no branch was left for the refused one

    other = guard.submit(SqlAction("DELETE FROM orders WHERE id <= 17"), scope, requester="quiet")
    assert other.outcome is Outcome.AWAITING_APPROVAL and other.action_id
    assert guard.store.count_pending("noisy") == 2 and guard.store.count_pending("quiet") == 1
    assert guard.store.count_pending() == 3


def test_the_overall_limit_still_applies_across_requesters(
    make_guard, store_path, proofs, orders, scope
):
    guard = make_guard(
        store=store_path, proof_verifier=proofs, max_pending=2, max_pending_per_requester=2
    )
    queue(guard, scope, requester="a")
    queue(guard, scope, "DELETE FROM orders WHERE id <= 16", requester="b")
    refused = guard.submit(SqlAction("DELETE FROM orders WHERE id <= 17"), scope, requester="c")
    assert (
        refused.outcome is Outcome.DENIED and "too many actions are waiting" in refused.reasons[-1]
    )


# -- a long check keeps its lease --------------------------------------------------------------


def test_reconcile_keeps_renewing_its_lease_while_it_checks(
    make_guard, store_path, proofs, orders, scope, monkeypatch
):
    lease = timedelta(seconds=0.3)
    guard = make_guard(store=store_path, proof_verifier=proofs, lease=lease)
    other = make_guard(store=store_path, proof_verifier=proofs, lease=lease)
    result = queue(guard, scope)
    crash_a_publish(guard, proofs, result, monkeypatch)
    time.sleep(0.45)  # the crashed publisher's lease runs out

    seen: dict[str, object] = {}

    def slow_judge(record, status):
        taken = guard.store.get(record.id).lease_until
        time.sleep(0.9)  # three lease lengths
        later = guard.store.get(record.id)
        seen["renewed"] = later.lease_until > taken
        seen["still_valid"] = later.lease_until > guard._clock()
        seen["other"] = other.reconcile()  # another process must find the lease fresh
        return A.NEEDS_ATTENTION, "checked slowly", True

    monkeypatch.setattr(guard, "_judge_publishing", slow_judge)
    [settled] = guard.reconcile()
    assert settled.state is A.NEEDS_ATTENTION
    assert seen == {"renewed": True, "still_valid": True, "other": []}


def test_the_lease_heartbeat_stops_with_the_reconcile(
    make_guard, store_path, proofs, orders, scope, monkeypatch
):
    guard = make_guard(store=store_path, proof_verifier=proofs, lease=timedelta(seconds=0.3))
    result = queue(guard, scope)
    crash_a_publish(guard, proofs, result, monkeypatch)
    time.sleep(0.45)
    before = {t.name for t in threading.enumerate()}
    assert [s.state for s in guard.reconcile()] == [A.NEEDS_ATTENTION]
    time.sleep(0.1)
    leaked = [t for t in threading.enumerate() if t.name.startswith("sqlguard-lease-")]
    assert not [t for t in leaked if t.name not in before]
