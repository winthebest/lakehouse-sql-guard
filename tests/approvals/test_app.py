"""The approval web app: the queue, an action's page, approving and rejecting, housekeeping."""

from __future__ import annotations

import threading
import time
from datetime import timedelta

import pyarrow as pa
import pytest

pytest.importorskip("starlette")
pytest.importorskip("httpx")
pytest.importorskip("jinja2")

from markupsafe import escape  # noqa: E402

from sqlguard.approvals.service import CANNOT_PROVE_GONE_HELP  # noqa: E402
from sqlguard.safety import ActionState, SqlAction  # noqa: E402
from sqlguard.safety.guard import Outcome  # noqa: E402
from tests.approvals.conftest import (  # noqa: E402
    Crash,
    branches,
    forms_of,
    post,
    queue,
    store_bytes,
)
from tests.safety.conftest import ROWS, order_ids  # noqa: E402

A = ActionState


def decide_form(client, action_id: str, decision: str = "approve", **extra: str) -> dict[str, str]:
    """What the browser would post for the button on the action's page."""
    page = client.get(f"/a/{action_id}")
    assert page.status_code == 200
    form = dict(forms_of(page.text).forms[f"/a/{action_id}/{decision}"])
    form.update(extra)
    return form


def apply_diff(base: pa.Table, diff) -> dict[int, dict]:
    """The table that results from applying a (complete) row diff to `base`, keyed by id."""
    rows = {row["id"]: row for row in base.to_pylist()}
    assert not diff.truncated
    for change in diff.rows:
        if change.change == "deleted":
            del rows[change.before["id"]]
        elif change.change == "added":
            rows[change.after["id"]] = dict(change.after)
        else:
            rows[change.key["id"]].update({c: change.after[c] for c in change.changed_columns})
    return rows


# -- reading -----------------------------------------------------------------------------------


def test_the_queue_lists_what_is_waiting(client, pguard, scope):
    result = queue(pguard, scope)
    page = client.get("/")
    assert page.status_code == 200
    assert f"/a/{result.action_id}" in page.text
    assert "db.orders" in page.text and "+0 -15" in page.text
    assert "Nothing is waiting" not in page.text
    assert f'hx-get="{client.prefix}/queue"' in page.text  # refreshed by the vendored htmx
    assert "<table" in client.get("/queue").text and "<html" not in client.get("/queue").text


def test_an_empty_queue_says_so(client):
    assert "Nothing is waiting." in client.get("/").text


def test_an_action_page_shows_the_statement_the_reasons_the_impact_and_the_diff(
    client, pguard, service, scope
):
    result = queue(pguard, scope)
    record = pguard.store.get(result.action_id)
    page = client.get(f"/a/{result.action_id}").text
    assert "DELETE FROM orders WHERE id &lt;= ?" in page  # literals are not shown
    assert record.action_hash[:16] in page and record.expires_at.strftime("%Y-%m-%d") in page
    assert all(str(escape(reason)) in page for reason in record.reasons)
    assert "<b>15</b> deleted" in page and "<b>0</b> added" in page
    assert "recomputed from the snapshots just now" in page
    shown = {c.before["id"] for c in service.view(result.action_id).diff.rows}
    assert shown == set(range(1, 16))
    assert "No invariants are configured" in page


def test_a_column_change_page_shows_the_schema_and_says_it_was_not_rehearsed(
    client, pguard, orders, scope
):
    result = queue(pguard, scope, "ALTER TABLE orders RENAME COLUMN customer TO buyer")
    page = client.get(f"/a/{result.action_id}").text
    assert "Not rehearsed" in page and "<h2>Schema change</h2>" in page
    assert "rename column customer to buyer" in page
    assert "<b>buyer</b> (renamed)" in page and "Row diff" not in page
    assert "None" not in page  # there is no branch head to show
    assert forms_of(page).disabled[f"/a/{result.action_id}/approve"] is False


def test_approving_a_column_change_applies_it_and_a_moved_main_blocks_it(
    client, pguard, orders, scope
):
    first = queue(pguard, scope, "ALTER TABLE orders RENAME COLUMN customer TO buyer")
    second = queue(pguard, scope, "ALTER TABLE orders DROP COLUMN amount")
    reply = post(client, f"/a/{first.action_id}/approve", decide_form(client, first.action_id))
    assert reply.status_code == 303
    assert "Approved: the change was published" in client.get(reply.headers["location"]).text
    assert [f.name for f in orders.table("orders").schema().fields] == ["id", "buyer", "amount"]
    # the first change moved the schema, so the second no longer matches what its reviewer saw
    page = client.get(f"/a/{second.action_id}").text
    assert "columns differ from the ones that were reviewed" in page
    assert forms_of(page).disabled[f"/a/{second.action_id}/approve"] is True


def test_a_write_reviewed_before_a_column_change_is_blocked_once_the_columns_changed(
    client, pguard, orders, scope
):
    update = queue(pguard, scope, "UPDATE orders SET amount = amount + 100 WHERE id <= 15")
    alter = queue(pguard, scope, "ALTER TABLE orders DROP COLUMN amount")
    post(client, f"/a/{alter.action_id}/approve", decide_form(client, alter.action_id))
    page = client.get(f"/a/{update.action_id}").text
    assert "columns changed after the review" in page
    assert forms_of(page).disabled[f"/a/{update.action_id}/approve"] is True


def test_the_diff_is_paged(client, pguard, orders, scope):
    orders.create_table("big", pa.table({"id": pa.array(range(100), pa.int64())}))
    result = pguard.submit(SqlAction("DELETE FROM big WHERE id < 80"), scope)
    assert result.outcome is Outcome.AWAITING_APPROVAL, result.reasons
    first = client.get(f"/a/{result.action_id}").text
    assert "Page 1 of 4" in first and "next" in first and "previous" not in first
    last = client.get(f"/a/{result.action_id}?page=4").text
    assert "Page 4 of 4" in last and "previous" in last
    assert client.get(f"/a/{result.action_id}?page=99").text.count("Page 4 of 4") == 1
    assert "Page 1 of 4" in client.get(f"/a/{result.action_id}?page=junk").text


def test_an_unknown_or_malformed_id_is_a_404(client):
    assert client.get("/a/" + "0" * 32).status_code == 404
    assert client.get("/a/not-an-id").status_code == 404
    assert post(client, "/a/not-an-id/approve", {}).status_code in (403, 404)


# -- deciding ----------------------------------------------------------------------------------


def test_a_delete_an_agent_proposed_is_applied_exactly_as_the_diff_showed(
    client, pguard, service, orders, scope
):
    result = queue(pguard, scope)
    base = orders.read("orders")
    page = client.get(f"/a/{result.action_id}")
    assert "<b>15</b> deleted" in page.text
    view = service.view(result.action_id)
    expected = apply_diff(base, view.diff)
    assert sorted(expected) == list(range(16, ROWS + 1))

    reply = post(client, f"/a/{result.action_id}/approve", decide_form(client, result.action_id))
    assert (
        reply.status_code == 303
        and reply.headers["location"] == f"{client.prefix}/a/{result.action_id}"
    )
    done = client.get(reply.headers["location"])
    assert "Approved: the change was published" in done.text
    assert 'class="pill published"' in done.text

    after = {row["id"]: row for row in orders.read("orders").to_pylist()}
    assert after == expected  # the table is the diff applied to the base, nothing else
    assert order_ids(orders) == list(range(16, ROWS + 1))
    record = pguard.store.get(result.action_id)
    assert record.state is A.PUBLISHED and record.approver == "alice"
    assert not branches(orders)
    assert b"id <= 15" not in store_bytes(pguard.store.path)  # the raw statement is erased


def test_an_update_is_applied_as_the_modified_rows_showed(
    make_client, make_guard, store_path, proofs, clock, orders, scope
):
    from sqlguard.approvals import ReviewService
    from sqlguard.safety import Policy

    keyed = Policy.from_dict({"invariants": {"default": {"unique_key": ["id"]}}})
    guard = make_guard(keyed, store=store_path, proof_verifier=proofs, clock=clock)
    service = ReviewService(guard, proofs, "alice", clock=clock)
    client = make_client(using=service)
    result = queue(guard, scope, "UPDATE orders SET amount = amount + 100 WHERE id <= 15")
    page = client.get(f"/a/{result.action_id}").text
    assert "<b>15</b> modified" in page and "<b>amount</b>" in page and "&rarr;" in page
    assert "unique_key" in page  # the invariants configured for the table are listed
    expected = apply_diff(orders.read("orders"), service.view(result.action_id).diff)
    post(client, f"/a/{result.action_id}/approve", decide_form(client, result.action_id))
    assert {r["id"]: r for r in orders.read("orders").to_pylist()} == expected


def test_rejecting_drops_the_branch_and_keeps_the_note_in_the_audit_log(
    client, pguard, orders, scope, audit
):
    result = queue(pguard, scope)
    assert branches(orders)
    form = decide_form(client, result.action_id, "reject", note="This deletes live orders.")
    reply = post(client, f"/a/{result.action_id}/reject", form)
    assert reply.status_code == 303
    assert "Rejected" in client.get(reply.headers["location"]).text
    assert pguard.store.get(result.action_id).state is A.REJECTED
    assert not branches(orders) and order_ids(orders) == list(range(1, ROWS + 1))
    notes = [r for r in audit.read() if r.get("outcome") == "reviewer_note"]
    assert notes[-1]["note"] == "This deletes live orders."
    assert notes[-1]["action_id"] == result.action_id and notes[-1]["resolved"] == "rejected"


@pytest.mark.parametrize("note", ["", "   \n\t "])
def test_rejecting_needs_a_note(client, pguard, orders, scope, note):
    result = queue(pguard, scope)
    form = decide_form(client, result.action_id, "reject", note=note)
    reply = post(client, f"/a/{result.action_id}/reject", form)
    assert reply.status_code == 400 and "note is required" in reply.text
    assert pguard.store.get(result.action_id).state is A.PENDING and branches(orders)
    # the proof was not spent: the same form works once it has a note
    form["note"] = "no"
    assert post(client, f"/a/{result.action_id}/reject", form).status_code == 303


def test_a_note_that_is_too_long_is_refused(client, pguard, scope):
    result = queue(pguard, scope)
    form = decide_form(client, result.action_id, "reject", note="x" * 1001)
    assert post(client, f"/a/{result.action_id}/reject", form).status_code == 400


def test_a_decided_action_cannot_be_decided_again(client, pguard, orders, scope):
    result = queue(pguard, scope)
    form = decide_form(client, result.action_id)
    assert post(client, f"/a/{result.action_id}/approve", form).status_code == 303
    again = post(client, f"/a/{result.action_id}/approve", form)
    assert again.status_code == 409 and "published" in again.text
    page = client.get(f"/a/{result.action_id}").text
    assert "Decision" not in page and "Approve</button>" not in page


def test_a_finished_action_is_not_reported_as_altered(client, pguard, orders, scope):
    """The statement is erased at the terminal state, which must not look like tampering."""
    result = queue(pguard, scope)
    assert post(client, f"/a/{result.action_id}/approve", decide_form(client, result.action_id))
    page = client.get(f"/a/{result.action_id}").text
    assert 'class="pill published"' in page
    assert "Approval is blocked" not in page and "altered" not in page


def test_an_approve_button_needs_a_pending_action(client, pguard, orders, scope):
    result = queue(pguard, scope)
    pguard.expire_pending(pguard._clock() + timedelta(days=2))
    assert 'class="pill expired"' in client.get(f"/a/{result.action_id}").text


# -- the live state ----------------------------------------------------------------------------


def test_a_table_that_moved_blocks_approval_and_ends_stale_if_forced(
    client, pguard, orders, scope, service
):
    result = queue(pguard, scope)
    form = decide_form(client, result.action_id)  # rendered while everything was in order
    orders.table("orders").append(
        pa.table({"id": pa.array([999], pa.int64()), "customer": ["late"], "amount": [1.0]})
    )
    page = client.get(f"/a/{result.action_id}").text
    assert "Approval is blocked" in page and "moved from snapshot" in page
    assert "proof" not in forms_of(page).forms[f"/a/{result.action_id}/approve"]
    assert forms_of(page).disabled[f"/a/{result.action_id}/approve"] is True

    reply = post(client, f"/a/{result.action_id}/approve", form)  # the old page's proof
    assert reply.status_code == 303
    assert "Stale" in client.get(reply.headers["location"]).text
    assert pguard.store.get(result.action_id).state is A.STALE
    assert 999 in order_ids(orders) and len(order_ids(orders)) == ROWS + 1  # nothing was deleted
    assert not branches(orders)


def test_a_changed_policy_blocks_approval(
    make_client, make_guard, store_path, proofs, clock, scope, pguard
):
    from sqlguard.approvals import ReviewService
    from sqlguard.safety import Policy

    result = queue(pguard, scope)
    other = make_guard(
        Policy.from_dict({"thresholds": {"max_affected_row_pct": 80}}),
        store=store_path,
        proof_verifier=proofs,
        clock=clock,
    )
    web = make_client(using=ReviewService(other, proofs, "alice", clock=clock))
    assert "differs from the one the action met" in web.get(f"/a/{result.action_id}").text


# -- housekeeping and needs_attention ----------------------------------------------------------


def test_housekeeping_runs_when_the_server_starts_and_then_periodically(
    make_client, service, monkeypatch
):
    runs = []
    real = service.housekeeping
    monkeypatch.setattr(service, "housekeeping", lambda: (runs.append(time.monotonic()), real())[1])
    client = make_client(housekeeping_seconds=0.05)
    with client:
        deadline = time.monotonic() + 10
        while len(runs) < 3 and time.monotonic() < deadline:
            time.sleep(0.02)
    assert len(runs) >= 3
    assert service.housekeeping_status.last_run is not None
    assert "Housekeeping last ran" in client.get("/").text


def test_housekeeping_expires_late_actions_and_survives_its_own_errors(
    service, pguard, clock, orders, scope, monkeypatch, caplog
):
    queue(pguard, scope)
    clock.advance(days=2)
    status = service.housekeeping()
    assert status.expired == 1 and status.last_error is None and not branches(orders)

    monkeypatch.setattr(pguard, "reconcile", lambda: (_ for _ in ()).throw(OSError("secret path")))
    status = service.housekeeping()
    assert status.last_error == "OSError"
    assert "secret path" not in repr(status)  # the error text stays in the log


def test_a_slow_housekeeping_pass_does_not_block_pages_or_decisions(
    service, pguard, scope, orders, monkeypatch
):
    result = queue(pguard, scope)
    started, release, done = threading.Event(), threading.Event(), threading.Event()
    real = pguard.reconcile

    def slow_reconcile(*args, **kwargs):
        started.set()
        release.wait(30)
        return real(*args, **kwargs)

    monkeypatch.setattr(pguard, "reconcile", slow_reconcile)
    sweep = threading.Thread(target=service.housekeeping)
    sweep.start()
    outcome = {}

    def reviewer():
        view = service.view(result.action_id, mint_proofs=True)
        outcome["decided"] = service.decide(result.action_id, "approve", view.approve_proof)
        done.set()

    assert started.wait(10)
    worker = threading.Thread(target=reviewer)
    worker.start()
    try:
        assert done.wait(10), "the page and the decision waited for housekeeping"
    finally:
        release.set()
        sweep.join(30)
        worker.join(30)
    assert outcome["decided"].result.outcome is Outcome.PUBLISHED
    assert order_ids(orders) == list(range(16, ROWS + 1))


def crash_the_publish(pguard, service, result, monkeypatch):
    def publish(branch, expected_head):
        raise Crash

    monkeypatch.setattr(pguard.adapter, "publish", publish)
    with pytest.raises(Crash):
        service.decide_with_new_proof(result.action_id, "approve")


def test_a_crashed_publish_shows_as_stuck_until_housekeeping_settles_it(
    client, service, pguard, clock, orders, scope, monkeypatch, linux_identity
):
    result = queue(pguard, scope)
    crash_the_publish(pguard, service, result, monkeypatch)
    assert pguard.store.get(result.action_id).state is A.PUBLISHING
    clock.advance(minutes=6)
    assert "stuck: its publisher stopped renewing the lease" in client.get("/").text
    assert service.housekeeping().settled == 1
    assert pguard.store.get(result.action_id).state is A.FAILED  # a provably dead publisher
    assert not branches(orders)


def test_needs_attention_comes_first_and_the_page_explains_why_on_a_mac(
    client, service, pguard, clock, orders, scope, monkeypatch, macos_identity
):
    crashed = queue(pguard, scope)
    crash_the_publish(pguard, service, crashed, monkeypatch)
    waiting = queue(pguard, scope, "DELETE FROM orders WHERE id <= 16")
    clock.advance(minutes=6)
    assert service.housekeeping().settled == 1
    record = pguard.store.get(crashed.action_id)
    assert record.state is A.NEEDS_ATTENTION and record.branch in branches(orders)
    assert "cannot be proven gone" in record.outcome_data["reason"]
    assert "another host" not in record.outcome_data["reason"]

    queue_page = client.get("/").text
    attention = queue_page.index("need attention")
    assert (
        attention < queue_page.index("Waiting for a decision") < queue_page.index(waiting.action_id)
    )
    assert queue_page.index(crashed.action_id) < queue_page.index("Waiting for a decision")
    assert 'role="alert"' in queue_page

    page = client.get(f"/a/{crashed.action_id}").text
    assert str(escape(CANNOT_PROVE_GONE_HELP)) in page
    assert "macOS has no boot id or PID namespace" in page
    assert f"<code>{record.base_snapshot_id}</code>" in page and record.branch in page
    assert "Decision" not in page  # nothing to decide: a person has to look at the table
