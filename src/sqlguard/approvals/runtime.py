"""Open the pieces a front end needs (store, catalog, policy, Guard) from command-line options."""

from __future__ import annotations

import argparse
import getpass
import os
from pathlib import Path

from sqlguard.approvals.proof import HmacProofs
from sqlguard.approvals.service import ReviewService, approver_label
from sqlguard.core.catalog import add_catalog_args, catalog_from_args
from sqlguard.safety import AuditError, Guard, Policy, StoreError
from sqlguard.safety.store import ActionStore

STORE_ENV = "SQLGUARD_STORE"
POLICY_ENV = "SQLGUARD_POLICY"
AUDIT_ENV = "SQLGUARD_AUDIT_LOG"


def add_runtime_args(parser: argparse.ArgumentParser) -> None:
    """The options every approvals command takes: where the actions are, and what they run on."""
    parser.add_argument(
        "--store",
        default=os.environ.get(STORE_ENV),
        help=f"action store file (env {STORE_ENV}); the one the agent's MCP server writes to",
    )
    add_catalog_args(parser)
    parser.add_argument(
        "--policy",
        default=os.environ.get(POLICY_ENV),
        help="policy YAML; must be the policy the actions were queued under "
        f"(env {POLICY_ENV}; default: strict built-in policy)",
    )
    parser.add_argument(
        "--audit-log",
        default=os.environ.get(AUDIT_ENV),
        help=f"audit log (env {AUDIT_ENV}; default: sqlguard-audit.jsonl in the --warehouse)",
    )
    parser.add_argument(
        "--approver",
        default=None,
        metavar="NAME",
        help="who is deciding, recorded in the store and the audit log (default: $USER). "
        "A label for the trail, not authentication.",
    )


def default_approver() -> str:
    try:
        return os.environ.get("USER") or getpass.getuser()
    except (KeyError, OSError):
        return "reviewer"


def open_store(args: argparse.Namespace, *, create: bool = False) -> ActionStore:
    if not args.store:
        raise ValueError(f"--store (or ${STORE_ENV}) is required")
    path = Path(args.store).expanduser()
    if not create and not path.is_file():
        raise ValueError(f"no action store at {path}")
    try:
        return ActionStore(path)
    except StoreError as err:
        raise ValueError(str(err)) from err


def open_service(args: argparse.Namespace, *, create_store: bool = False) -> ReviewService:
    """A `ReviewService` over a Guard with this process's own proof key."""
    store = open_store(args, create=create_store)
    catalog = catalog_from_args(args)
    if catalog is None:
        raise ValueError("--catalog NAME or --warehouse PATH is required")
    if args.warehouse and not (Path(args.warehouse).expanduser() / "catalog.db").is_file():
        raise ValueError(f"no Iceberg warehouse at {args.warehouse} (catalog.db not found)")
    try:
        policy = Policy.load(args.policy) if args.policy else Policy()
    except OSError as err:
        raise ValueError(f"cannot load policy: {err}") from err
    audit_path = args.audit_log or (
        str(Path(args.warehouse).expanduser() / "sqlguard-audit.jsonl") if args.warehouse else None
    )
    if audit_path is None:
        raise ValueError(f"--audit-log (or ${AUDIT_ENV}) is required with --catalog")
    proofs = HmacProofs()
    try:
        guard = Guard(catalog, policy, audit_path=audit_path, store=store, proof_verifier=proofs)
        guard.audit.ensure_writable()
    except AuditError as err:
        raise ValueError(str(err)) from err
    return ReviewService(guard, proofs, args.approver or default_approver())


def validated_label(text: str | None) -> str:
    return approver_label(text or default_approver())
