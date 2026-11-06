"""The end-to-end demo's reviewer client reads the real approval UI: the pages the app serves
are parsed with docker/demo-e2e/reviewer.py, so a change of the templates that breaks the demo
fails here."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip("starlette")
pytest.importorskip("httpx")
pytest.importorskip("jinja2")

from tests.approvals.conftest import post, queue  # noqa: E402

PATH = Path(__file__).parents[2] / "docker" / "demo-e2e" / "reviewer.py"


def load_reviewer():
    spec = importlib.util.spec_from_file_location("demo_e2e_reviewer", PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up here
    spec.loader.exec_module(module)
    return module


demo = load_reviewer()


def test_the_queue_page_gives_the_full_id_and_state(client, pguard, scope):
    result = queue(pguard, scope)
    rows = demo.parse_queue(client.get("/queue").text)
    assert rows == [demo.QueueRow(result.action_id, "pending")]


def test_an_action_page_gives_the_diff_the_reasons_and_the_forms(client, pguard, scope):
    result = queue(pguard, scope)
    page = demo.parse_action(client.get(f"/a/{result.action_id}").text, result.action_id)
    assert page.state == "pending" and page.table == "db.orders"
    assert page.counts.startswith("0 added") and "15 deleted" in page.counts
    assert len(page.rows) == 15 and page.rows[0].startswith("deleted ")
    assert "id=1" in page.rows[0]
    assert page.reasons and not page.problems
    assert page.base_snapshot.isdigit() and page.head_snapshot.isdigit()
    approve, reject = page.forms["approve"], page.forms["reject"]
    assert approve.enabled and reject.enabled
    assert set(approve.fields) == {"csrf_token", "proof"}
    assert approve.action.endswith(f"/a/{result.action_id}/approve")


def test_deciding_with_the_pages_own_form_and_reading_the_notice(client, pguard, scope):
    result = queue(pguard, scope)
    page = demo.parse_action(client.get(f"/a/{result.action_id}").text, result.action_id)
    form = {**page.forms["reject"].fields, "note": "too many rows"}
    reply = post(client, f"/a/{result.action_id}/reject", form)
    assert reply.status_code == 303
    after = demo.parse_action(client.get(reply.headers["location"]).text, result.action_id)
    assert after.state == "rejected" and after.flashes[0].startswith("Rejected")
    assert not after.problems  # a finished action is not reported as tampered with
    assert not after.forms


def test_the_sign_in_link_is_found_in_the_servers_output():
    text = (
        "Approval UI on port 18765 (deciding as demo-reviewer)\n"
        "Open this link in your browser and confirm to sign in; it works once:\n"
        "  http://127.0.0.1:18765/s/AbC-123_xyz/login?token=tok-EN_1\n"
    )
    link = demo.parse_signin_link(text)
    assert (link.host, link.port, link.base, link.token) == (
        "127.0.0.1",
        18765,
        "/s/AbC-123_xyz",
        "tok-EN_1",
    )
    with pytest.raises(ValueError, match="no sign-in link"):
        demo.parse_signin_link("Traceback (most recent call last): ...")
