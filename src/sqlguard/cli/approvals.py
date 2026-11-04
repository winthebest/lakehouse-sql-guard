"""`sqlguard approvals ...` and `sqlguard serve`: decide the actions an agent left pending.

    sqlguard approvals list   --store S [--all] [--json]
    sqlguard approvals show   ID --store S (--catalog NAME | --warehouse DIR) [--policy P] [--json]
    sqlguard approvals approve ID --store S (--catalog NAME | --warehouse DIR) [--note TEXT]
    sqlguard approvals reject ID --note TEXT --store S (--catalog NAME | --warehouse DIR)
    sqlguard serve --store S (--catalog NAME | --warehouse DIR) [--policy P] [--port 8765]

`approve` and `reject` only work at an interactive terminal, and `approve` makes you retype the
first 8 characters of the action's hash after reading its summary. The proof `Guard.resolve`
needs is minted by this very process after that confirmation; the key never touches disk. This
is deliberate friction against an agent that runs the command for you, not authentication.

Exit codes: 0 = done, 1 = not applied (blocked, stale, declined, failed), 2 = usage or input error.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import socket
import sys
from collections.abc import Callable
from typing import Any

from sqlguard.approvals.runtime import STORE_ENV, add_runtime_args, open_service, open_store
from sqlguard.approvals.service import counts_of
from sqlguard.cli import register
from sqlguard.safety import ActionNotPending, ActionState, ProofRejected

TYPED_PREFIX = 8
SHOWN_ROWS = 10


def is_interactive() -> bool:
    """True when both stdin and stdout are a terminal (the CLI will not decide otherwise)."""
    return sys.stdin.isatty() and sys.stdout.isatty()


def _read_line(prompt: str) -> str:
    try:
        return input(prompt)
    except EOFError:  # input closed under us: that is a "no"
        return ""


def _require_terminal(command: str) -> None:
    if not is_interactive():
        raise ValueError(
            f"`approvals {command}` needs an interactive terminal (stdin and stdout); "
            "use the web UI or run it yourself in a terminal"
        )


# -- list ------------------------------------------------------------------------------------


def _list(args: argparse.Namespace) -> int:
    store = open_store(args)
    states = (
        None
        if args.all
        else [ActionState.PENDING, ActionState.PUBLISHING, ActionState.NEEDS_ATTENTION]
    )
    records = store.list_actions(states, limit=args.limit)
    if args.json:
        print(json.dumps([_summary(r) for r in records], indent=2))
        return 0
    if not records:
        print("no actions" if args.all else "nothing is waiting")
        return 0
    print(f"{'ID':<10} {'STATE':<16} {'TABLE':<24} {'CHANGE':<14} {'REQUESTER':<12} DEADLINE")
    for r in records:
        counts = counts_of(r)
        print(
            f"{r.id[:8]:<10} {r.state.value:<16} {r.table[:24]:<24} "
            f"{(counts.text if counts else r.kind):<14} {r.requester[:12]:<12} "
            f"{r.expires_at:%Y-%m-%d %H:%MZ}"
        )
    return 0


def _summary(record: Any) -> dict[str, Any]:
    counts = counts_of(record)
    return {
        "id": record.id,
        "state": record.state.value,
        "kind": record.kind,
        "table": record.table,
        "requester": record.requester,
        "expires_at": record.expires_at.isoformat(),
        "action_hash": record.action_hash,
        "rows_added": counts.added if counts else None,
        "rows_deleted": counts.deleted if counts else None,
        "rows_modified": counts.modified if counts else None,
    }


# -- show / approve / reject -------------------------------------------------------------------


def _print_view(view: Any, rows: int = SHOWN_ROWS) -> None:
    r = view.record
    print(f"action      {r.id}")
    print(f"state       {r.state.value}")
    print(f"table       {r.table} ({r.kind})")
    print(f"requester   {r.requester}")
    print(f"deadline    {r.expires_at:%Y-%m-%d %H:%M:%SZ}")
    print(f"snapshots   base {r.base_snapshot_id} -> head {r.head_snapshot_id}")
    print(f"action hash {r.action_hash}")
    print(f"statement   {view.sql}")
    for reason in r.reasons:
        print(f"  policy: {reason}")
    if view.diff is not None:
        counts = view.counts
        print(f"row diff    {counts.text} ({view.diff.mode}; recomputed just now)")
        for change in view.diff.rows[:rows]:
            print(f"  {change.change:<9} {_row(change)}")
        if len(view.diff.rows) > rows:
            print(f"  ... {len(view.diff.rows) - rows} more listed rows; the web UI shows them all")
    elif view.reviewed is not None:
        print(f"row diff    {view.reviewed.text} (as reviewed; not recomputed)")
    for line in view.help:
        print(f"note: {line}")
    for problem in view.problems:
        print(f"BLOCKED: {problem}")


def _row(change: Any) -> str:
    if change.change == "added":
        data = change.after or {}
    elif change.change == "deleted":
        data = change.before or {}
    else:
        data = {
            c: f"{(change.before or {}).get(c)} -> {(change.after or {}).get(c)}"
            for c in change.changed_columns
        }
    key = f"[{', '.join(f'{k}={v}' for k, v in (change.key or {}).items())}] " if change.key else ""
    return key + ", ".join(f"{k}={v}" for k, v in data.items())


def _show(args: argparse.Namespace) -> int:
    service = open_service(args)
    view = service.view(service.find(args.id).id)
    if args.json:
        print(
            json.dumps(
                {
                    "action": view.record.to_dict(),
                    "statement": view.sql,
                    "problems": list(view.problems),
                    "row_diff": view.diff.to_dict() if view.diff else None,
                    "can_approve": view.can_approve,
                },
                indent=2,
                default=str,
            )
        )
    else:
        _print_view(view)
    return 0


def _decide(args: argparse.Namespace, decision: str) -> int:
    _require_terminal(decision)
    service = open_service(args)
    view = service.view(service.find(args.id).id)
    _print_view(view)
    if not view.can_decide:
        print(f"\nnot decided: the action is {view.record.state.value}", file=sys.stderr)
        return 1
    if decision == "approve":
        if not view.can_approve:
            print("\nnot approved: fix or reject it first (see BLOCKED above)", file=sys.stderr)
            return 1
        expected = view.record.action_hash[:TYPED_PREFIX]
        typed = _read_line(
            f"\nType the first {TYPED_PREFIX} characters of the action hash shown above to "
            "approve (anything else cancels): "
        ).strip()
        if typed.lower() != expected:
            print("cancelled: that is not the start of the action hash", file=sys.stderr)
            return 1
    try:
        decided = service.decide_with_new_proof(view.record.id, decision, args.note)  # type: ignore[arg-type]
    except (ActionNotPending, ProofRejected) as err:
        print(f"not decided: {err}", file=sys.stderr)
        return 1
    result = decided.result
    print(f"\n{result.outcome.value}")
    for reason in result.reasons:
        print(f"  {reason}")
    if not decided.note_recorded:
        print("warning: the note could not be written to the audit log", file=sys.stderr)
    return 0 if result.ok or result.outcome.value == "rejected" else 1


def _approve(args: argparse.Namespace) -> int:
    return _decide(args, "approve")


def _reject(args: argparse.Namespace) -> int:
    return _decide(args, "reject")


# -- serve -------------------------------------------------------------------------------------


def _port_busy(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host, port))
        except OSError:
            return True
    return False


def run_server(app: Any, host: str, port: int) -> None:
    """Serve `app` until interrupted. Access logging is off: it would record URLs."""
    import uvicorn

    uvicorn.run(app, host=host, port=port, log_level="warning", access_log=False)


def _serve(args: argparse.Namespace) -> int:
    from sqlguard.approvals.security import UNSAFE_FLAG, AccessControl, check_bind_host

    host = check_bind_host(args.host, unsafe=args.i_know_this_is_unauthenticated)
    if args.housekeeping_interval < 5:
        raise ValueError("--housekeeping-interval must be at least 5 seconds")
    try:
        from sqlguard.approvals.app import create_app
    except ImportError as err:
        raise ValueError(
            "the approval UI needs the `ui` extra: pip install 'lakehouse-sql-guard[ui]'"
        ) from err
    service = open_service(args, create_store=True)
    if _port_busy(host, args.port):
        raise ValueError(
            f"port {args.port} on {host} is in use; stop whatever holds it "
            f"(see `lsof -i :{args.port}`) or choose --port"
        )
    access = AccessControl()
    app = create_app(
        service, access, bind_host=host, housekeeping_seconds=args.housekeeping_interval
    )
    shown = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    if host != "127.0.0.1":
        print(
            f"WARNING: listening on {host}; only the token protects it ({UNSAFE_FLAG}).", flush=True
        )
    print(f"Approval UI on port {args.port} (deciding as {service.approver})", flush=True)
    print("Open this link in your browser and confirm to sign in; it works once:", flush=True)
    print(f"  http://{shown}:{args.port}{access.base}/login?token={access.token}", flush=True)
    with contextlib.suppress(KeyboardInterrupt):
        run_server(app, host, args.port)
    return 0


# -- registration ------------------------------------------------------------------------------


def add_parser(subparsers: Any) -> argparse.ArgumentParser:
    approvals = subparsers.add_parser(
        "approvals", help="list, show, approve or reject the actions an agent left pending"
    )
    sub = approvals.add_subparsers(dest="command", required=True)

    listing = sub.add_parser("list", help="the queue (pending, publishing, needs attention)")
    listing.add_argument("--store", default=os.environ.get(STORE_ENV))
    listing.add_argument("--all", action="store_true", help="include finished actions")
    listing.add_argument("--limit", type=int, default=50)
    listing.add_argument("--json", action="store_true")

    for name, text in (
        ("show", "one action with its row diff recomputed from the snapshots"),
        ("approve", "approve an action (terminal only; retype the start of its hash)"),
        ("reject", "reject an action (terminal only; a note is required)"),
    ):
        command = sub.add_parser(name, help=text)
        command.add_argument("id", help="action id, or a unique prefix of at least 4 characters")
        add_runtime_args(command)
        if name == "show":
            command.add_argument("--json", action="store_true")
        else:
            command.add_argument(
                "--note",
                required=name == "reject",
                help="why (required to reject; recorded in the audit log)",
            )
    return approvals


def add_serve_parser(subparsers: Any) -> argparse.ArgumentParser:
    serve = subparsers.add_parser("serve", help="local web UI to review and decide pending actions")
    add_runtime_args(serve)
    serve.add_argument("--host", default="127.0.0.1", help="address to bind (default: 127.0.0.1)")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument(
        "--housekeeping-interval",
        type=float,
        default=60.0,
        metavar="SECONDS",
        help="how often to settle crashed publishes, expire late actions and sweep orphaned "
        "branches (default: 60)",
    )
    serve.add_argument(
        "--i-know-this-is-unauthenticated",
        action="store_true",
        help="allow --host other than 127.0.0.1",
    )
    return serve


_COMMANDS: dict[str, Callable[[argparse.Namespace], int]] = {
    "list": _list,
    "show": _show,
    "approve": _approve,
    "reject": _reject,
}


def handle(args: argparse.Namespace) -> int:
    return _COMMANDS[args.command](args)


register("approvals", add_parser, handle)
register("serve", add_serve_parser, _serve)
