"""What the approval server refuses: strangers, forged requests, forged or reused proofs, edits
made to the store behind its back, and markup smuggled into what it shows."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pyarrow as pa
import pytest

pytest.importorskip("starlette")
pytest.importorskip("httpx")
pytest.importorskip("jinja2")


from sqlguard.approvals import HmacProofs  # noqa: E402
from sqlguard.approvals.app import make_environment  # noqa: E402
from sqlguard.approvals.security import (  # noqa: E402
    AccessControl,
    allowed_hosts,
    check_bind_host,
)
from sqlguard.safety import ActionState, ProofRequest, SqlAction  # noqa: E402
from sqlguard.safety.guard import Outcome  # noqa: E402
from tests.approvals.conftest import (  # noqa: E402
    ORIGIN,
    branches,
    edit_row,
    forms_of,
    post,
    queue,
    rehash,
    sign_in,
    store_bytes,
)
from tests.safety.conftest import ROWS, order_ids  # noqa: E402

A = ActionState
POST_PATHS = ("approve", "reject")


def form_for(client, action_id: str, decision: str = "approve", **extra: str) -> dict[str, str]:
    page = client.get(f"/a/{action_id}")
    form = dict(forms_of(page.text).forms[f"/a/{action_id}/{decision}"])
    form.update(extra)
    return form


def untouched(orders, guard, result) -> None:
    assert guard.store.get(result.action_id).state is A.PENDING
    assert order_ids(orders) == list(range(1, ROWS + 1))


# -- who may talk to the server ------------------------------------------------------------------


@pytest.mark.parametrize("host", ["evil.example", "127.0.0.1.evil.example", "192.168.1.5", ""])
def test_a_foreign_host_header_is_refused(make_client, pguard, scope, host):
    result = queue(pguard, scope)
    client = make_client()
    for path in ("/", f"/a/{result.action_id}", "/queue", "/audit"):
        assert client.get(path, headers={"Host": host}).status_code == 400
    assert client.get("/", headers={"Host": "localhost:8765"}).status_code == 200


def test_a_foreign_host_cannot_even_redeem_the_token(make_client, access):
    client = make_client(signed_in=False)
    reply = client.post(
        "/login",
        data={"token": access.token},
        headers={"Host": "evil.example", "Origin": "http://evil.example"},
    )
    assert reply.status_code == 400 and not client.cookies
    assert sign_in(client, access.token).status_code == 303  # the token was not spent


@pytest.mark.parametrize("path", ["/", "/queue", "/audit", "/a/" + "0" * 32])
def test_without_a_session_nothing_is_served(make_client, path):
    client = make_client(signed_in=False)
    assert client.get(path).status_code == 401
    client.cookies.set("sqlguard_session", "guess")
    assert client.get(path).status_code == 401
    assert post(client, path + "/approve", {}).status_code == 401


def test_a_wrong_token_is_refused_and_does_not_burn_the_real_one(make_client, access):
    client = make_client(signed_in=False)
    for guess in ("", "wrong", access.token[:-1], access.token + "x"):
        assert post(client, "/login", {"token": guess}).status_code == 401
    assert not client.cookies
    assert post(client, "/login", {"token": access.token}).status_code == 303


def test_the_token_works_exactly_once(make_client, access):
    first = make_client(signed_in=False)
    assert sign_in(first, access.token).status_code == 303
    second = make_client(signed_in=False)
    assert post(second, "/login", {"token": access.token}).status_code == 401
    assert second.get("/").status_code == 401
    assert first.get("/").status_code == 200  # the first session is unaffected


def test_a_program_holding_only_the_cookie_value_gets_nothing(
    client, make_client, access, pguard, orders, scope
):
    """Browsers send a cookie to every port of 127.0.0.1, so another local server (one the agent
    started, that the person visits) learns its value. It does not learn the secret path."""
    result = queue(pguard, scope)
    leaked = client.cookies.get("sqlguard_session")
    thief = make_client(signed_in=False)
    thief.raw = True  # does not know the prefix
    thief.cookies.set("sqlguard_session", leaked)
    wrong = "/s/" + ("A" * 32)
    near_miss = access.base[:-1] + ("A" if access.base[-1] != "A" else "B")
    id_path = f"/a/{result.action_id}"
    bodies = set()
    for prefix in ("", "/s", "/s/", wrong, near_miss, access.base[:-3]):
        for path in (prefix + "/", prefix + id_path, prefix + "/audit", prefix + "/queue"):
            reply = thief.get(path)
            assert reply.status_code == 404, (path, reply.status_code)
            bodies.add(reply.text)
        reply = thief.post(prefix + id_path + "/approve", data={}, headers={"Origin": ORIGIN})
        assert reply.status_code == 404
        bodies.add(reply.text)
    assert bodies == {"Not Found"}  # nothing that hints a UI is here, or what the prefix is
    assert pguard.store.get(result.action_id).state is A.PENDING
    assert order_ids(orders) == list(range(1, ROWS + 1))


def test_the_cookie_is_scoped_to_the_secret_path_so_a_browser_sends_it_nowhere_else(
    make_client, access
):
    client = make_client(signed_in=False)
    cookie = sign_in(client, access.token).headers["set-cookie"]
    assert f"Path={access.base}/" in cookie
    secret = access.base.removeprefix("/s/")
    assert len(secret) >= 22 and all(c.isalnum() or c in "-_" for c in secret)  # >= 128 bits
    assert secret not in "".join(f"{k}={v}" for k, v in cookie_pairs(cookie))  # not in its value
    assert AccessControl().base != AccessControl().base  # fresh at every start


def cookie_pairs(header: str):
    return [tuple(part.strip().split("=", 1)) for part in header.split(";") if "=" in part][:1]


def test_looking_at_the_sign_in_page_does_not_spend_the_token(make_client, access):
    client = make_client(signed_in=False)
    link = {"token": access.token}
    assert client.get("/login", params=link).status_code == 200
    assert client.head("/login", params=link).status_code == 200  # a preview or prefetch
    assert client.get("/login", params=link).status_code == 200
    page = client.get("/login", params=link).text
    assert f'name="token" value="{access.token}"' in page and "Sign in" in page
    assert client.get("/login").status_code == 401  # no token, nothing to confirm
    assert not client.cookies
    assert post(client, "/login", {"token": access.token}).status_code == 303  # only a POST
    assert post(client, "/login", {"token": access.token}).status_code == 401


def test_signing_in_again_replaces_the_session_it_was_made_from():
    access = AccessControl("t" * 40)
    first = access.redeem("t" * 40)
    other = AccessControl("u" * 40)
    old = other.redeem("u" * 40)
    assert access.session(first.id) is first
    # a token is single use, so rotation is shown on an instance that allows a second redeem
    access._used = False
    second = access.redeem("t" * 40, replacing=first.id)
    assert second.id != first.id and second.csrf != first.csrf
    assert access.session(first.id) is None and access.session(second.id) is second
    assert other.session(old.id) is old  # unrelated sessions are untouched


def test_the_session_cookie_is_httponly_samesite_strict_and_unguessable(make_client, access):
    client = make_client(signed_in=False)
    reply = sign_in(client, access.token)
    cookie = reply.headers["set-cookie"]
    assert "HttpOnly" in cookie and "samesite=strict" in cookie.lower()
    assert f"Path={access.base}/;" in cookie or cookie.endswith(f"Path={access.base}/")
    value = cookie.split(";")[0].split("=", 1)[1]
    assert len(value) >= 32 and value != access.token
    assert "token" not in reply.headers.get("location", "")


def test_responses_carry_headers_that_stop_framing_sniffing_and_foreign_scripts(client):
    headers = client.get("/").headers
    csp = headers["content-security-policy"]
    assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert "default-src 'none'" in csp and "unsafe-inline" not in csp and "unsafe-eval" not in csp
    assert headers["x-frame-options"] == "DENY" and headers["x-content-type-options"] == "nosniff"
    assert headers["referrer-policy"] == "no-referrer" and headers["cache-control"] == "no-store"
    assert "content-security-policy" in client.get("/login", params={"token": "x"}).headers


def test_a_non_loopback_bind_is_refused_unless_the_person_insists():
    assert (
        check_bind_host("127.0.0.1") == "127.0.0.1" and check_bind_host("localhost") == "127.0.0.1"
    )
    for host in ("0.0.0.0", "192.168.1.5", "::1", "example.com", ""):
        with pytest.raises(ValueError, match="i-know-this-is-unauthenticated"):
            check_bind_host(host)
    assert check_bind_host("192.168.1.5", unsafe=True) == "192.168.1.5"
    assert allowed_hosts("127.0.0.1") == ["127.0.0.1", "localhost"]
    assert "192.168.1.5" in allowed_hosts("192.168.1.5")


# -- forged requests -----------------------------------------------------------------------------


@pytest.mark.parametrize("decision", POST_PATHS)
def test_a_post_without_the_csrf_token_is_refused(client, pguard, orders, scope, decision):
    result = queue(pguard, scope)
    form = form_for(client, result.action_id, decision, note="because")
    del form["csrf_token"]
    assert post(client, f"/a/{result.action_id}/{decision}", form).status_code == 403
    form["csrf_token"] = "forged"
    assert post(client, f"/a/{result.action_id}/{decision}", form).status_code == 403
    form["csrf_token"] = ""
    assert post(client, f"/a/{result.action_id}/{decision}", form).status_code == 403
    untouched(orders, pguard, result)


@pytest.mark.parametrize("decision", POST_PATHS)
@pytest.mark.parametrize(
    "origin", [None, "null", "https://evil.example", "http://127.0.0.1:9999", ORIGIN + ".evil"]
)
def test_a_post_from_another_origin_is_refused(client, pguard, orders, scope, decision, origin):
    result = queue(pguard, scope)
    form = form_for(client, result.action_id, decision, note="because")
    assert post(client, f"/a/{result.action_id}/{decision}", form, origin=origin).status_code == 403
    untouched(orders, pguard, result)


def test_another_sessions_csrf_token_is_not_accepted(make_client, access, pguard, orders, scope):
    from sqlguard.approvals.security import AccessControl

    result = queue(pguard, scope)
    mine = make_client()
    form = form_for(mine, result.action_id)
    other = AccessControl()
    theirs = other.redeem(other.token)
    assert theirs.csrf != mine.cookies.get("sqlguard_session")
    form["csrf_token"] = theirs.csrf
    assert post(mine, f"/a/{result.action_id}/approve", form).status_code == 403
    untouched(orders, pguard, result)


def test_every_post_route_is_covered_by_the_csrf_tests(client):
    """A new POST route must be added to POST_PATHS above (and so to the CSRF/Origin tests)."""
    posts = set()
    for mount in client.app.routes:
        for route in mount.routes:
            if "POST" in (getattr(route, "methods", None) or ()):
                posts.add(route.path.rsplit("/", 1)[-1])
    assert posts == set(POST_PATHS) | {"login", "logout"}  # login has no session to protect


def test_signing_out_needs_the_csrf_token_and_ends_the_session(client, access):
    token = forms_of(client.get("/").text).forms["/logout"]["csrf_token"]
    assert post(client, "/logout", {"x": "y"}).status_code == 403
    assert post(client, "/logout", {"csrf_token": "forged"}).status_code == 403
    assert client.get("/").status_code == 200
    reply = post(client, "/logout", {"csrf_token": token})
    assert reply.status_code == 200 and "Signed out" in reply.text
    assert "max-age=0" in reply.headers["set-cookie"].lower()
    client.cookies.set("sqlguard_session", "x")  # whatever the browser still holds is useless
    assert client.get("/").status_code == 401


def test_only_a_form_post_is_taken(client, pguard, scope):
    result = queue(pguard, scope)
    path = f"/a/{result.action_id}/approve"
    headers = {"Origin": ORIGIN}
    assert client.post(path, json={"x": 1}, headers=headers).status_code == 415
    assert client.put(path, headers=headers).status_code == 405
    assert client.delete(path, headers=headers).status_code == 405
    big = {"csrf_token": "x", "note": "n" * 20_000}
    assert post(client, path, big).status_code == 413


# -- proofs --------------------------------------------------------------------------------------


def test_a_proof_cannot_be_used_twice(client, pguard, orders, scope):
    result = queue(pguard, scope)
    form = form_for(client, result.action_id)
    assert post(client, f"/a/{result.action_id}/approve", form).status_code == 303
    again = post(client, f"/a/{result.action_id}/approve", form)
    assert again.status_code == 409
    assert order_ids(orders) == list(range(16, ROWS + 1))
    assert pguard.store.proof_used(
        hashlib.sha256(b"sqlguard-proof\0" + form["proof"].encode()).hexdigest()
    )
    assert form["proof"].encode() not in store_bytes(pguard.store.path)  # only its hash is kept


def test_a_spent_proof_is_refused_by_the_guard_even_for_a_pending_action(
    pguard, proofs, service, orders, scope
):
    """The store spends a proof once; a verifier that would accept it again does not matter."""
    first, second = queue(pguard, scope), queue(pguard, scope, "DELETE FROM orders WHERE id <= 16")
    request = ProofRequest(first.action_id, first.action_hash, "reject", "alice")
    proof = proofs.mint(request)
    assert pguard.resolve(first.action_id, "reject", "alice", proof).outcome is Outcome.REJECTED

    class AcceptsEverything:
        def verify(self, request, proof):
            return True

    pguard.proof_verifier = AcceptsEverything()
    from sqlguard.safety import ProofRejected

    with pytest.raises(ProofRejected):
        pguard.resolve(second.action_id, "reject", "alice", proof)
    assert pguard.store.get(second.action_id).state is A.PENDING


def test_a_proof_from_another_secret_is_refused(client, pguard, orders, scope):
    result = queue(pguard, scope)
    record = pguard.store.get(result.action_id)
    forged = HmacProofs().mint(ProofRequest(record.id, record.action_hash, "approve", "alice"))
    form = form_for(client, result.action_id, proof=forged)
    reply = post(client, f"/a/{result.action_id}/approve", form)
    assert reply.status_code == 403 and "invalid, expired or was already used" in reply.text
    untouched(orders, pguard, result)


@pytest.mark.parametrize(
    "proof", ["", "v1.a.b.c", "v1." + "a" * 32 + ".99999999999.deadbeef", "x" * 300, "v2.n.1.m"]
)
def test_a_malformed_proof_is_refused(client, pguard, orders, scope, proof):
    result = queue(pguard, scope)
    form = form_for(client, result.action_id, proof=proof)
    assert post(client, f"/a/{result.action_id}/approve", form).status_code == 403
    untouched(orders, pguard, result)


def test_a_reject_proof_cannot_approve_and_vice_versa(client, pguard, orders, scope):
    result = queue(pguard, scope)
    reject_proof = form_for(client, result.action_id, "reject")["proof"]
    approve_proof = form_for(client, result.action_id, "approve")["proof"]
    swapped = form_for(client, result.action_id, "approve", proof=reject_proof)
    assert post(client, f"/a/{result.action_id}/approve", swapped).status_code == 403
    swapped = form_for(client, result.action_id, "reject", proof=approve_proof, note="no")
    assert post(client, f"/a/{result.action_id}/reject", swapped).status_code == 403
    untouched(orders, pguard, result)


def test_a_proof_for_another_action_is_refused(client, pguard, orders, scope):
    one, two = queue(pguard, scope), queue(pguard, scope, "DELETE FROM orders WHERE id <= 16")
    other = form_for(client, two.action_id)["proof"]
    form = form_for(client, one.action_id, proof=other)
    assert post(client, f"/a/{one.action_id}/approve", form).status_code == 403
    untouched(orders, pguard, one)


def test_a_proof_for_another_approver_is_refused(client, pguard, proofs, orders, scope):
    result = queue(pguard, scope)
    mallory = proofs.mint(ProofRequest(result.action_id, result.action_hash, "approve", "mallory"))
    form = form_for(client, result.action_id, proof=mallory)
    assert post(client, f"/a/{result.action_id}/approve", form).status_code == 403
    untouched(orders, pguard, result)


def test_a_proof_expires(client, pguard, clock, orders, scope):
    result = queue(pguard, scope)
    form = form_for(client, result.action_id)
    clock.advance(minutes=16)  # the proofs, the service and the Guard share this clock
    reply = post(client, f"/a/{result.action_id}/approve", form)
    assert reply.status_code == 403
    assert pguard.store.get(result.action_id).state is A.PENDING


def test_the_proof_secret_is_never_written_or_shown(
    client, pguard, proofs, access, orders, scope, audit_path, caplog
):
    caplog.set_level("DEBUG")
    result = queue(pguard, scope)
    form = form_for(client, result.action_id)
    post(client, f"/a/{result.action_id}/approve", form)
    secret = proofs._secret
    assert "hidden" in repr(proofs) and secret.hex() not in repr(proofs)
    assert "hidden" in repr(access) and access.token not in repr(access)
    everything = store_bytes(pguard.store.path) + Path(audit_path).read_bytes()
    everything += caplog.text.encode()
    for forbidden in (secret, secret.hex().encode(), access.token.encode(), form["proof"].encode()):
        assert forbidden not in everything


def test_secrets_are_too_short_to_be_accepted():
    with pytest.raises(ValueError):
        HmacProofs(b"short")


# -- what the store can no longer be believed about -----------------------------------------------


def test_main_moving_after_the_page_was_shown_ends_stale_not_published(
    client, pguard, orders, scope
):
    result = queue(pguard, scope)
    form = form_for(client, result.action_id)
    orders.table("orders").append(
        pa.table({"id": pa.array([500], pa.int64()), "customer": ["x"], "amount": [1.0]})
    )
    reply = post(client, f"/a/{result.action_id}/approve", form)
    assert reply.status_code == 303
    assert pguard.store.get(result.action_id).state is A.STALE
    assert sorted(order_ids(orders)) == [*range(1, ROWS + 1), 500] and not branches(orders)


def test_a_diff_digest_edited_in_the_store_is_noticed_and_approval_is_blocked(
    client, pguard, orders, scope, store_path
):
    result = queue(pguard, scope)
    approve_form = form_for(client, result.action_id)  # a proof minted before the edit
    edit_row(store_path, result.action_id, diff_digest="0" * 64)
    rehash(pguard, result.action_id, store_path)  # someone who knows the hash formula

    page = client.get(f"/a/{result.action_id}").text
    assert "differs from the one that was reviewed" in page and "Approval is blocked" in page
    forms = forms_of(page)
    assert forms.disabled[f"/a/{result.action_id}/approve"] is True
    assert "proof" not in forms.forms[f"/a/{result.action_id}/approve"]

    # the proof minted before the edit is bound to the old hash and is useless now
    assert post(client, f"/a/{result.action_id}/approve", approve_form).status_code == 403
    assert order_ids(orders) == list(range(1, ROWS + 1))
    assert pguard.store.get(result.action_id).state is A.PENDING


def test_a_record_edited_without_rehashing_is_noticed(client, pguard, scope, store_path):
    result = queue(pguard, scope)
    edit_row(store_path, result.action_id, diff_digest="0" * 64)
    page = client.get(f"/a/{result.action_id}").text
    assert "no longer matches its hash" in page and "Approval is blocked" in page


def test_stored_counts_that_were_edited_do_not_change_what_is_shown(
    client, pguard, scope, store_path
):
    result = queue(pguard, scope)
    record = pguard.store.get(result.action_id)
    impact = record.impact_data
    impact["row_diff"]["rows_deleted"] = 1
    import json

    edit_row(store_path, result.action_id, impact_json=json.dumps(impact))
    rehash(pguard, result.action_id, store_path)
    page = client.get(f"/a/{result.action_id}").text
    assert "<b>15</b> deleted" in page  # recomputed from the snapshots
    assert "The counts stored when it was queued were +0 -1" in page


def test_a_display_copy_of_the_sql_that_was_edited_is_not_trusted(
    client, pguard, scope, store_path
):
    result = queue(pguard, scope)
    edit_row(store_path, result.action_id, sql_redacted="SELECT 1")
    rehash(pguard, result.action_id, store_path)
    page = client.get(f"/a/{result.action_id}").text
    assert "DELETE FROM orders" in page and "SELECT 1" not in page
    assert "display differs from the SQL that will run" in page


def audit_branch(guard, result):
    from sqlguard.safety.executor.base import BranchHandle

    record = guard.store.get(result.action_id)
    return BranchHandle(
        record.table, record.branch, record.base_snapshot_id, record.head_snapshot_id
    )


def test_a_branch_that_vanished_blocks_approval(client, pguard, orders, scope):
    result = queue(pguard, scope)
    pguard.adapter.drop_branch(audit_branch(pguard, result))
    page = client.get(f"/a/{result.action_id}").text
    assert "audit branch" in page and "Approval is blocked" in page
    assert forms_of(page).disabled[f"/a/{result.action_id}/approve"] is True


def test_a_branch_the_agent_wrote_to_after_the_review_blocks_approval(
    client, pguard, orders, scope
):
    result = queue(pguard, scope)
    form = form_for(client, result.action_id)
    pguard.adapter.apply_sql(audit_branch(pguard, result), "DELETE FROM orders WHERE id = 20")
    page = client.get(f"/a/{result.action_id}").text
    assert "audit branch moved" in page and "Approval is blocked" in page
    reply = post(client, f"/a/{result.action_id}/approve", form)  # even with the earlier proof
    assert reply.status_code == 303
    assert pguard.store.get(result.action_id).state is A.STALE
    assert order_ids(orders) == list(range(1, ROWS + 1))


def test_a_record_edited_after_the_page_was_shown_ends_stale(
    client, pguard, orders, scope, store_path
):
    result = queue(pguard, scope)
    form = form_for(client, result.action_id)
    edit_row(store_path, result.action_id, diff_digest="0" * 64)  # not re-hashed
    assert post(client, f"/a/{result.action_id}/approve", form).status_code == 303
    assert pguard.store.get(result.action_id).state is A.STALE
    assert order_ids(orders) == list(range(1, ROWS + 1))


# -- markup in data -----------------------------------------------------------------------------


def test_the_templates_escape_everything_by_default():
    env = make_environment()
    assert env.autoescape is True
    assert env.from_string("{{ x }}").render(x="<script>alert(1)</script>") == (
        "&lt;script&gt;alert(1)&lt;/script&gt;"
    )


def test_markup_in_cell_values_and_column_names_is_escaped(
    make_client, pguard, orders, scope, service
):
    payload = "<script>alert('x')</script>"
    orders.create_table(
        "weird",
        pa.table(
            {
                "id": pa.array(range(1, 11), pa.int64()),
                "<i>name</i>": pa.array([payload] * 10),
            }
        ),
    )
    result = pguard.submit(SqlAction("DELETE FROM weird WHERE id <= 8"), scope)
    assert result.outcome is Outcome.AWAITING_APPROVAL, result.reasons
    client = make_client()
    page = client.get(f"/a/{result.action_id}").text
    assert "<script>alert" not in page and "&lt;script&gt;alert(" in page
    assert "<i>name</i>" not in page and "&lt;i&gt;name&lt;/i&gt;" in page
    assert page.count("<script") == 1  # only the vendored htmx tag in the base template
    queue_page = client.get("/").text
    assert "<script>alert" not in queue_page


def test_markup_in_the_sql_text_and_reasons_is_escaped(make_client, pguard, scope, store_path):
    result = queue(pguard, scope)
    evil = "<img src=x onerror=alert(1)>"
    edit_row(store_path, result.action_id, reasons_json=f'["{evil}"]', requester=evil)
    rehash(pguard, result.action_id, store_path)
    client = make_client()
    for page in (client.get(f"/a/{result.action_id}").text, client.get("/").text):
        assert evil not in page and "&lt;img src=x onerror=alert(1)&gt;" in page


def test_markup_in_a_note_is_escaped_on_the_audit_page(client, pguard, scope):
    result = queue(pguard, scope)
    note = "<script>alert('n')</script>"
    form = form_for(client, result.action_id, "reject", note=note)
    assert post(client, f"/a/{result.action_id}/reject", form).status_code == 303
    page = client.get("/audit").text
    assert note not in page and "&lt;script&gt;alert(" in page


def test_the_audit_page_never_shows_raw_sql(
    make_guard, store_path, proofs, clock, scope, make_client
):
    from sqlguard.approvals import ReviewService

    keeping = make_guard(
        store=store_path, proof_verifier=proofs, clock=clock, redact_sql_literals=False
    )
    keeping.submit(SqlAction("DELETE FROM orders WHERE customer = 'zz-private-1'"), scope)
    page = (
        make_client(using=ReviewService(keeping, proofs, "alice", clock=clock)).get("/audit").text
    )
    assert "zz-private-1" not in page and "DELETE FROM orders" in page


# -- nothing is fetched from elsewhere -------------------------------------------------------------


def test_the_pages_load_nothing_from_other_origins(client, pguard, scope):
    result = queue(pguard, scope)
    for path in ("/", f"/a/{result.action_id}", "/audit"):
        page = client.get(path).text
        assert "http://" not in page and "https://" not in page and "//cdn" not in page
        assert f'<script src="{client.prefix}/static/htmx.min.js"' in page


def test_the_vendored_htmx_is_the_release_that_was_checked(client):
    served = client.get("/static/htmx.min.js")
    assert served.status_code == 200
    # htmx.org 2.0.11 (npm tarball sha1 a85363c4c5af94cd634df04c4b83d06dd0e91aa7)
    assert hashlib.sha256(served.content).hexdigest() == (
        "d6fdc75f204e6bdefa99b69bf1e6d4ac69b8a364f77929f45c13476b4000f717"
    )
    assert client.get("/static/..%2f..%2fsecurity.py").status_code in (400, 404)
    assert client.get("/static/app.css").status_code == 200


def test_unknown_methods_and_paths_do_not_leak_a_trace(client):
    assert client.get("/nope").status_code == 404
    assert "Traceback" not in client.get("/nope").text
    assert client.request("TRACE", "/", headers={"Origin": ORIGIN}).status_code == 405


def test_an_access_control_forgets_expired_sessions():
    now = [0.0]
    access = AccessControl("t" * 40, clock=lambda: now[0])
    session = access.redeem("t" * 40)
    assert access.session(session.id) is session
    now[0] += 9 * 3600
    assert access.session(session.id) is None
