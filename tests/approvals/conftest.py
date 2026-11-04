"""Fixtures for the approval tests: a Guard with a store over a throwaway warehouse, the service,
and (when the `ui` extra and httpx are installed) a signed-in client for the web app."""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path

import pytest

from sqlguard.approvals import HmacProofs, ReviewService
from sqlguard.safety import ActionState, Guard, SqlAction
from sqlguard.safety.guard import Outcome
from tests.safety import conftest as _safety

# the safety tests' fixtures, available here under their own names
audit, audit_path, make_guard, orders, scope = (
    _safety.audit,
    _safety.audit_path,
    _safety.make_guard,
    _safety.orders,
    _safety.scope,
)

RISKY = "DELETE FROM orders WHERE id <= 15"  # 75% of the rows: the rehearsal says ask
ORIGIN = "http://127.0.0.1:8765"
APPROVER = "alice"
A = ActionState


class Clock:
    def __init__(self) -> None:
        self.now = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


class Crash(BaseException):
    """Stands in for the process being killed: nothing in the Guard may catch it."""


@pytest.fixture
def linux_identity(monkeypatch):
    """A boot id and PID namespace as on Linux, so a dead pid can be proven."""
    import sqlguard.safety.guard as guard_module

    monkeypatch.setattr(guard_module, "_boot_id", lambda: "boot-1")
    monkeypatch.setattr(guard_module, "_pid_namespace", lambda: "pid:[1]")


@pytest.fixture
def macos_identity(monkeypatch):
    """No boot id and no PID namespace, as on macOS: a dead publisher cannot be proven dead."""
    import sqlguard.safety.guard as guard_module

    monkeypatch.setattr(guard_module, "_boot_id", lambda: None)
    monkeypatch.setattr(guard_module, "_pid_namespace", lambda: None)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store_path(tmp_path: Path) -> Path:
    return tmp_path / "state" / "actions.db"


@pytest.fixture
def proofs(clock) -> HmacProofs:
    return HmacProofs(clock=clock)


@pytest.fixture
def pguard(make_guard, store_path, proofs, clock) -> Guard:
    return make_guard(store=store_path, proof_verifier=proofs, clock=clock)


@pytest.fixture
def service(pguard, proofs, clock) -> ReviewService:
    return ReviewService(pguard, proofs, APPROVER, clock=clock)


def queue(guard: Guard, task_scope, sql: str = RISKY, requester: str | None = None):
    result = guard.submit(SqlAction(sql), task_scope, requester=requester)
    assert result.outcome is Outcome.AWAITING_APPROVAL and result.action_id, result.reasons
    return result


def edit_row(path: Path, action_id: str, **columns) -> None:
    conn = sqlite3.connect(path)
    sets = ", ".join(f'"{c}" = ?' for c in columns)
    conn.execute(f"UPDATE actions SET {sets} WHERE id = ?", [*columns.values(), action_id])
    conn.commit()
    conn.close()


def rehash(guard: Guard, action_id: str, path: Path) -> None:
    """Make the stored hash agree with the (edited) row, as someone who knows the formula would."""
    edit_row(path, action_id, action_hash=guard.store.get(action_id).computed_hash())


def store_bytes(path: Path) -> bytes:
    return b"".join(
        Path(f).read_bytes() for f in (path, f"{path}-wal", f"{path}-shm") if Path(f).exists()
    )


def branches(catalog, table: str = "orders") -> set[str]:
    return set(catalog.refs(table)) - {"main"}


class _Forms(HTMLParser):
    """The forms of a page: action -> {input name: value}."""

    def __init__(self) -> None:
        super().__init__()
        self.forms: dict[str, dict[str, str]] = {}
        self._current: dict[str, str] | None = None
        self.disabled: dict[str, bool] = {}
        self._action = ""

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form":
            self._action = a.get("action", "")
            self._current = self.forms.setdefault(self._action, {})
        elif tag == "input" and self._current is not None and "name" in a:
            self._current[a["name"]] = a.get("value", "")
        elif tag == "button" and self._current is not None:
            self.disabled[self._action] = "disabled" in a

    def handle_endtag(self, tag):
        if tag == "form":
            self._current = None


def forms_of(html: str) -> _Forms:
    parser = _Forms()
    parser.feed(html)
    return parser


def text_of(html: str) -> str:
    """The visible text of a page, roughly: tags dropped, entities left as served."""
    return re.sub(r"<[^>]+>", " ", html)


@pytest.fixture
def access():
    from sqlguard.approvals.security import AccessControl

    return AccessControl()


@pytest.fixture
def make_client(service, access):
    from starlette.testclient import TestClient

    from sqlguard.approvals.app import create_app

    clients = []

    def build(
        *, using=None, signed_in: bool = True, housekeeping_seconds=None, **kwargs
    ) -> TestClient:
        app = create_app(
            using or service, access, housekeeping_seconds=housekeeping_seconds, **kwargs
        )
        client = TestClient(app, base_url=ORIGIN, follow_redirects=False)
        clients.append(client)
        if signed_in:
            reply = client.get("/login", params={"token": access.token})
            assert reply.status_code == 303, reply.text
        return client

    yield build
    for client in clients:
        client.close()


@pytest.fixture
def client(make_client):
    return make_client()


def post(client, path: str, data: dict[str, str], *, origin: str | None = ORIGIN):
    headers = {"Origin": origin} if origin is not None else {}
    return client.post(path, data=data, headers=headers)
