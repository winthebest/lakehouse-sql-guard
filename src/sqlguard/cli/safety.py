"""`sqlguard safety ...`: pre-execution decision for agent SQL."""

from __future__ import annotations

import argparse
import json

from sqlguard.cli import register
from sqlguard.core.catalog import add_catalog_args, catalog_from_args
from sqlguard.core.findings import Decision


def safety_check(args: argparse.Namespace) -> int:
    from sqlguard.safety import Policy, SqlAction, TaskScope, classify

    catalog = catalog_from_args(args)  # only supplies the namespace of bare table names
    policy = Policy.load(args.policy) if args.policy else Policy()
    scope = TaskScope.of(
        tables=args.scope or (),
        namespaces=args.namespace or (),
        **({"default_namespace": catalog.namespace} if catalog else {}),
    )
    classification = classify(SqlAction(args.sql, dialect=args.dialect), catalog)
    decision = policy.decide(classification, scope)
    payload = {
        "decision": decision.decision.value,
        "class": decision.klass.value,
        "reasons": list(decision.reasons),
        "statements": [
            {"index": s.index, "verb": s.verb, "class": s.klass.value, "reasons": list(s.reasons)}
            for s in classification.statements
        ],
    }
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"{payload['decision'].upper()}  ({payload['class']})")
        for reason in payload["reasons"]:
            print(f"    - {reason}")
    return 0 if decision.decision in (Decision.ALLOW, Decision.ALLOW_VIA_BRANCH) else 1


_COMMANDS = {"check": safety_check}


def add_parser(subparsers) -> argparse.ArgumentParser:
    safety = subparsers.add_parser("safety", help="pre-execution decision for agent SQL")
    safety_sub = safety.add_subparsers(dest="command", required=True)
    decide = safety_sub.add_parser("check", help="classify one SQL action and apply the policy")
    decide.add_argument("sql")
    decide.add_argument("--policy", help="policy YAML (default: strict built-in policy)")
    decide.add_argument("--scope", nargs="*", help="tables the task may touch, e.g. db.orders")
    decide.add_argument("--namespace", nargs="*", help="namespaces the task may touch")
    decide.add_argument("--dialect", default="duckdb")
    add_catalog_args(decide)
    decide.add_argument("--json", action="store_true")
    return safety


def handle(args: argparse.Namespace) -> int:
    return _COMMANDS[args.command](args)


register("safety", add_parser, handle)
