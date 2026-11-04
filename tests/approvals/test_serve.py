"""`sqlguard serve`: what it refuses to start, and what it prints for the person who ran it."""

from __future__ import annotations

import re
import socket

import pytest

pytest.importorskip("starlette")
pytest.importorskip("httpx")

from starlette.testclient import TestClient  # noqa: E402

from sqlguard.cli import approvals as cli_approvals  # noqa: E402
from sqlguard.cli import main  # noqa: E402
from tests.approvals.conftest import ORIGIN, queue  # noqa: E402


@pytest.fixture
def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def serve(orders, free_port, tmp_path, store_path, audit_path, capsys, monkeypatch):
    started = []
    monkeypatch.setattr(
        cli_approvals, "run_server", lambda app, host, port: started.append((app, host, port))
    )

    def go(*extra: str, store=None) -> tuple[int, str, str, list]:
        argv = [
            "serve",
            "--store",
            str(store or store_path),
            "--warehouse",
            str(tmp_path / "warehouse"),
        ]
        argv += ["--audit-log", str(audit_path), "--approver", "alice"]
        if "--port" not in extra:
            argv += ["--port", str(free_port)]
        argv += list(extra)
        with pytest.raises(SystemExit) as stop:
            main(argv)
        out = capsys.readouterr()
        return stop.value.code, out.out, out.err, started

    return go


def test_serve_prints_a_one_time_link_and_hands_the_app_to_the_server(
    serve, pguard, scope, free_port
):
    result = queue(pguard, scope)
    code, out, _, started = serve()
    assert code == 0 and len(started) == 1
    app, host, port = started[0]
    assert (host, port) == ("127.0.0.1", free_port)
    links = re.findall(rf"http://127\.0\.0\.1:{free_port}/login\?token=([\w-]+)", out)
    assert len(links) == 1 and len(links[0]) >= 32
    assert "deciding as alice" in out and "works once" in out

    with TestClient(app, base_url=ORIGIN, follow_redirects=False) as browser:
        assert browser.get("/").status_code == 401
        assert browser.get("/login", params={"token": links[0]}).status_code == 303
        assert result.action_id in browser.get("/").text
        other = TestClient(app, base_url=ORIGIN, follow_redirects=False)
        assert other.get("/login", params={"token": links[0]}).status_code == 401


def test_each_start_prints_a_different_token(serve):
    tokens = {re.search(r"token=([\w-]+)", serve()[1]).group(1) for _ in range(3)}
    assert len(tokens) == 3


def test_serve_refuses_to_listen_beyond_loopback(serve):
    for host in ("0.0.0.0", "192.168.1.5", "::"):
        code, out, err, started = serve("--host", host)
        assert code == 2 and "i-know-this-is-unauthenticated" in err and not started
        assert "token=" not in out + err


def test_serve_beyond_loopback_needs_the_flag_and_says_so(serve, free_port):
    code, out, _, started = serve("--host", "0.0.0.0", "--i-know-this-is-unauthenticated")
    assert code == 0 and started[0][1] == "0.0.0.0"
    assert "WARNING" in out and f"http://127.0.0.1:{free_port}/login?token=" in out


def test_serve_refuses_a_port_that_is_taken_instead_of_picking_another(serve):
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        port = taken.getsockname()[1]
        code, out, err, started = serve("--port", str(port))
    assert code == 2 and "in use" in err and "lsof" in err and not started
    assert "token=" not in out


def test_serve_needs_the_ui_extra_and_says_how_to_get_it(serve, monkeypatch):
    monkeypatch.setitem(__import__("sys").modules, "sqlguard.approvals.app", None)
    code, _, err, started = serve()
    assert code == 2 and "lakehouse-sql-guard[ui]" in err and not started


def test_the_housekeeping_interval_has_a_floor(serve):
    code, _, err, _ = serve("--housekeeping-interval", "1")
    assert code == 2 and "at least 5 seconds" in err


def test_serve_creates_the_store_when_the_agent_has_not_queued_anything_yet(serve, tmp_path):
    fresh = tmp_path / "new-state" / "actions.db"
    code, _, _, started = serve(store=fresh)
    assert code == 0 and fresh.exists() and (fresh.stat().st_mode & 0o777) == 0o600
    assert started
