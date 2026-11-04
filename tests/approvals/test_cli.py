"""`sqlguard approvals ...`: the terminal gate, the retyped hash prefix, and what it decides."""

from __future__ import annotations

import json
import sys

import pytest

from sqlguard.cli import approvals as cli_approvals
from sqlguard.cli import main
from sqlguard.safety import ActionState
from tests.approvals.conftest import branches, queue
from tests.safety.conftest import ROWS, order_ids

A = ActionState


@pytest.fixture
def argv(tmp_path, store_path, audit_path):
    """The options that point the CLI at the same warehouse, store and audit log as `pguard`."""

    def build(*command: str, approver: str | None = "alice") -> list[str]:
        base = ["--store", str(store_path), "--warehouse", str(tmp_path / "warehouse")]
        base += ["--audit-log", str(audit_path)]
        if approver:
            base += ["--approver", approver]
        return [*command, *base]

    return build


@pytest.fixture
def run(capsys):
    def go(*args: str) -> tuple[int, str, str]:
        with pytest.raises(SystemExit) as stop:
            main(list(args))
        captured = capsys.readouterr()
        return stop.value.code, captured.out, captured.err

    return go


@pytest.fixture
def terminal(monkeypatch):
    """Pretend stdin and stdout are a terminal and that the person types `typed[0]`."""
    typed: list[str] = []
    prompts: list[str] = []

    def read(prompt: str) -> str:
        prompts.append(prompt)
        return typed.pop(0) if typed else ""

    monkeypatch.setattr(cli_approvals, "is_interactive", lambda: True)
    monkeypatch.setattr(cli_approvals, "_read_line", read)
    return typed, prompts


# -- looking ------------------------------------------------------------------------------------


def test_list_shows_the_queue_and_hides_what_is_finished(pguard, scope, run, tmp_path, store_path):
    result = queue(pguard, scope)
    code, out, _ = run("approvals", "list", "--store", str(store_path))
    assert code == 0 and result.action_id[:8] in out and "db.orders" in out and "+0 -15" in out
    code, out, _ = run("approvals", "list", "--store", str(store_path), "--json")
    listed = json.loads(out)
    assert listed[0]["id"] == result.action_id and listed[0]["rows_deleted"] == 15
    assert listed[0]["action_hash"] == result.action_hash


def test_list_includes_finished_actions_on_request(pguard, scope, run, store_path, service):
    result = queue(pguard, scope)
    service.decide_with_new_proof(result.action_id, "reject", "no")
    _, out, _ = run("approvals", "list", "--store", str(store_path))
    assert "nothing is waiting" in out
    _, out, _ = run("approvals", "list", "--store", str(store_path), "--all")
    assert "rejected" in out


def test_a_missing_store_is_an_error_not_a_new_empty_store(run, tmp_path):
    missing = tmp_path / "nowhere" / "actions.db"
    code, _, err = run("approvals", "list", "--store", str(missing))
    assert code == 2 and "no action store" in err and not missing.exists()
    code, _, err = run("approvals", "list")
    assert code == 2 and "--store" in err


def test_show_prints_the_hash_the_statement_and_the_recomputed_diff(pguard, scope, run, argv):
    result = queue(pguard, scope)
    code, out, _ = run("approvals", *argv("show", result.action_id))
    assert code == 0
    assert result.action_hash in out and "DELETE FROM orders WHERE id <= ?" in out
    assert "+0 -15" in out and "recomputed just now" in out and "deleted" in out
    assert "BLOCKED" not in out
    code, out, _ = run("approvals", *argv("show", result.action_id, "--json"))
    shown = json.loads(out)
    assert shown["can_approve"] is True and shown["row_diff"]["rows_deleted"] == 15
    assert "proof" not in out


def test_an_id_prefix_works_when_it_is_unique_and_long_enough(pguard, scope, run, argv):
    result = queue(pguard, scope)
    assert run("approvals", *argv("show", result.action_id[:6]))[0] == 0
    code, _, err = run("approvals", *argv("show", result.action_id[:2]))
    assert code == 2 and "no action" in err
    code, _, err = run("approvals", *argv("show", "0" * 8))
    assert code == 2 and "no action" in err


def test_a_prefix_shared_by_two_actions_is_ambiguous(pguard, scope, run, argv, store_path):
    from tests.approvals.conftest import edit_row

    first, second = queue(pguard, scope), queue(pguard, scope, "DELETE FROM orders WHERE id <= 16")
    shared = first.action_id[:6]
    edit_row(store_path, second.action_id, id=shared + "0" * 26)
    edit_row(store_path, first.action_id, id=shared + "1" * 26)
    code, _, err = run("approvals", *argv("show", shared))
    assert code == 2 and "matches 2 actions" in err and "type more of the id" in err
    assert run("approvals", *argv("show", shared + "11"))[0] == 0
    assert run("approvals", *argv("show", shared + "0" * 26))[0] == 0


# -- approving ------------------------------------------------------------------------------------


def test_approve_refuses_without_a_terminal(pguard, scope, run, argv, orders):
    result = queue(pguard, scope)
    code, _, err = run("approvals", *argv("approve", result.action_id))
    assert code == 2 and "interactive terminal" in err
    assert pguard.store.get(result.action_id).state is A.PENDING
    assert order_ids(orders) == list(range(1, ROWS + 1))


def test_approve_needs_both_stdin_and_stdout_to_be_terminals(monkeypatch):
    class Fake:
        def __init__(self, tty: bool) -> None:
            self.tty = tty

        def isatty(self) -> bool:
            return self.tty

    for stdin, stdout, expected in ((True, True, True), (True, False, False), (False, True, False)):
        monkeypatch.setattr(sys, "stdin", Fake(stdin))
        monkeypatch.setattr(sys, "stdout", Fake(stdout))
        assert cli_approvals.is_interactive() is expected


def test_approve_refuses_a_wrong_prefix_and_applies_nothing(
    pguard, scope, run, argv, terminal, orders
):
    typed, prompts = terminal
    result = queue(pguard, scope)
    for wrong in ("", "zzzzzzzz", result.action_hash[:7], result.action_hash[1:9], "yes"):
        typed.append(wrong)
        code, _, err = run("approvals", *argv("approve", result.action_id))
        assert code == 1 and "not the start of the action hash" in err
    assert len(prompts) == 5
    assert pguard.store.get(result.action_id).state is A.PENDING
    assert order_ids(orders) == list(range(1, ROWS + 1)) and branches(orders)


def test_the_hash_of_another_action_is_not_accepted(pguard, scope, run, argv, terminal):
    typed, _ = terminal
    first, second = queue(pguard, scope), queue(pguard, scope, "DELETE FROM orders WHERE id <= 16")
    typed.append(second.action_hash[:8])
    code, _, _ = run("approvals", *argv("approve", first.action_id))
    assert code == 1 and pguard.store.get(first.action_id).state is A.PENDING


def test_approve_with_the_right_prefix_applies_the_reviewed_change(
    pguard, scope, run, argv, terminal, orders, audit
):
    typed, prompts = terminal
    result = queue(pguard, scope)
    typed.append(result.action_hash[:8].upper())  # case does not matter
    code, out, _ = run("approvals", *argv("approve", result.action_id, "--note", "checked"))
    assert code == 0 and "published" in out
    assert "action hash" in out and result.action_hash in out  # shown before it asks
    assert result.action_hash not in prompts[0]  # ... but the prompt does not hand it over
    record = pguard.store.get(result.action_id)
    assert record.state is A.PUBLISHED and record.approver == "alice"
    assert order_ids(orders) == list(range(16, ROWS + 1)) and not branches(orders)
    note = [r for r in audit.read() if r.get("outcome") == "reviewer_note"][-1]
    assert note["note"] == "checked" and note["approver"] == "alice"
    assert "v1." not in out  # a proof is never printed


def test_approve_is_blocked_when_the_table_moved_and_does_not_ask(
    pguard, scope, run, argv, terminal, orders
):
    import pyarrow as pa

    typed, prompts = terminal
    result = queue(pguard, scope)
    orders.table("orders").append(
        pa.table({"id": pa.array([77], pa.int64()), "customer": ["x"], "amount": [1.0]})
    )
    code, out, err = run("approvals", *argv("approve", result.action_id))
    assert code == 1 and "BLOCKED" in out and "not approved" in err and prompts == []
    assert pguard.store.get(result.action_id).state is A.PENDING


def test_approve_of_a_decided_action_does_nothing(pguard, scope, run, argv, terminal, service):
    typed, prompts = terminal
    result = queue(pguard, scope)
    service.decide_with_new_proof(result.action_id, "reject", "no")
    code, _, err = run("approvals", *argv("approve", result.action_id))
    assert code == 1 and "rejected" in err and prompts == []


def test_approve_that_goes_stale_exits_nonzero(
    pguard, scope, run, argv, terminal, orders, store_path
):
    from tests.approvals.conftest import edit_row

    typed, _ = terminal
    result = queue(pguard, scope)
    typed.append(result.action_hash[:8])
    edit_row(store_path, result.action_id, diff_digest="0" * 64)  # not re-hashed
    code, out, _ = run("approvals", *argv("approve", result.action_id))
    assert code == 1 and "BLOCKED" in out
    assert order_ids(orders) == list(range(1, ROWS + 1))


# -- rejecting ------------------------------------------------------------------------------------


def test_reject_needs_a_note(pguard, scope, run, argv, terminal):
    result = queue(pguard, scope)
    code, _, err = run("approvals", *argv("reject", result.action_id))
    assert code == 2 and "--note" in err
    code, _, err = run("approvals", *argv("reject", result.action_id, "--note", "  "))
    assert code == 2 and "note is required" in err
    assert pguard.store.get(result.action_id).state is A.PENDING


def test_reject_also_needs_a_terminal(pguard, scope, run, argv):
    result = queue(pguard, scope)
    code, _, err = run("approvals", *argv("reject", result.action_id, "--note", "no"))
    assert code == 2 and "interactive terminal" in err
    assert pguard.store.get(result.action_id).state is A.PENDING


def test_reject_drops_the_branch_and_records_the_note(
    pguard, scope, run, argv, terminal, orders, audit
):
    result = queue(pguard, scope)
    assert branches(orders)
    code, out, _ = run("approvals", *argv("reject", result.action_id, "--note", "too broad"))
    assert code == 0 and "rejected" in out
    assert pguard.store.get(result.action_id).state is A.REJECTED and not branches(orders)
    assert order_ids(orders) == list(range(1, ROWS + 1))
    assert [r["note"] for r in audit.read() if r.get("outcome") == "reviewer_note"] == ["too broad"]


def test_the_approver_label_defaults_to_the_user(pguard, scope, run, argv, terminal, monkeypatch):
    monkeypatch.setenv("USER", "bob")
    result = queue(pguard, scope)
    code, _, _ = run("approvals", *argv("reject", result.action_id, "--note", "n", approver=None))
    assert code == 0 and pguard.store.get(result.action_id).approver == "bob"


def test_a_label_that_is_not_printable_is_refused(pguard, scope, run, argv, terminal):
    result = queue(pguard, scope)
    code, _, err = run(
        "approvals", *argv("reject", result.action_id, "--note", "n", approver="a\x00b")
    )
    assert code == 2 and "approver label" in err
