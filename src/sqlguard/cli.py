"""`sqlguard` command line.

    sqlguard leakage check queries/ --spec spec.yaml [--warehouse DIR --replay] [--json]
    sqlguard safety check "DELETE FROM orders" --policy policy.yaml --scope db.orders [--json]

Exit codes: 0 = clean / allowed, 1 = leaky or not allowed, 2 = usage or input error.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from sqlguard.core.findings import Decision


def _sql_files(target: Path) -> list[Path]:
    if target.is_dir():
        return sorted(target.rglob("*.sql"))
    return [target]


def leakage_check(args: argparse.Namespace) -> int:
    from sqlguard.core.catalog import LakehouseCatalog
    from sqlguard.leakage.replay import Replayer
    from sqlguard.leakage.spec import LeakageSpec
    from sqlguard.leakage.validator import Weights, validate

    spec = LeakageSpec.from_yaml(args.spec)
    catalog = LakehouseCatalog.local(args.warehouse) if args.warehouse else None
    schema = None
    if catalog is None:
        if not args.schema:
            print("error: pass --warehouse (Iceberg catalog) or --schema (YAML)", file=sys.stderr)
            return 2
        import yaml

        schema = yaml.safe_load(Path(args.schema).read_text(encoding="utf-8"))
    if args.replay and catalog is None:
        print("error: --replay needs --warehouse", file=sys.stderr)
        return 2
    weights = Weights.load(args.weights)
    files = [f for target in args.targets for f in _sql_files(Path(target))]
    if not files:
        print(f"error: no .sql files under {' '.join(args.targets)}", file=sys.stderr)
        return 2
    replay = Replayer(catalog, spec) if args.replay else False  # one replayer, shared cache
    worst = 0
    results = []
    for path in files:
        report = validate(
            path.read_text(encoding="utf-8"),
            spec,
            schema=schema,
            catalog=catalog,
            replay=replay,
            weights=weights,
        )
        results.append({"file": str(path), **report.to_dict()})
        failing = report.verdict == "leaky" or (
            report.verdict == "unverifiable" and not args.allow_unverifiable
        )
        worst = max(worst, int(failing))
        if not args.json:
            print(f"{report.verdict.upper():13} {report.score:.2f}  {path}")
            for finding in report.findings:
                where = f" [{finding.location}]" if finding.location else ""
                print(f"    {finding.rule_id:12} {finding.message}{where}")
    if args.json:
        print(json.dumps(results, indent=2, default=str))
    return worst


def safety_check(args: argparse.Namespace) -> int:
    from sqlguard.safety import Policy, SqlAction, TaskScope, classify

    policy = Policy.load(args.policy) if args.policy else Policy()
    scope = TaskScope.of(tables=args.scope or (), namespaces=args.namespace or ())
    classification = classify(SqlAction(args.sql, dialect=args.dialect))
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sqlguard", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="module", required=True)

    leakage = sub.add_parser("leakage", help="temporal leakage in training-set SQL")
    leakage_sub = leakage.add_subparsers(dest="command", required=True)
    check = leakage_sub.add_parser("check", help="check .sql files or directories of them")
    check.add_argument("targets", nargs="+", metavar="target")
    check.add_argument("--spec", required=True, help="LeakageSpec YAML")
    check.add_argument("--warehouse", help="local Iceberg warehouse (schema + snapshots)")
    check.add_argument("--schema", help="YAML {table: {column: type}} when no warehouse")
    check.add_argument("--replay", action="store_true", help="point-in-time replay")
    check.add_argument("--weights", help="scoring weights YAML")
    check.add_argument(
        "--allow-unverifiable",
        action="store_true",
        help="exit 0 for queries the guard could not fully check (default: fail closed)",
    )
    check.add_argument("--json", action="store_true")
    check.set_defaults(handler=leakage_check)

    safety = sub.add_parser("safety", help="pre-execution decision for agent SQL")
    safety_sub = safety.add_subparsers(dest="command", required=True)
    decide = safety_sub.add_parser("check", help="classify one SQL action and apply the policy")
    decide.add_argument("sql")
    decide.add_argument("--policy", help="policy YAML (default: strict built-in policy)")
    decide.add_argument("--scope", nargs="*", help="tables the task may touch, e.g. db.orders")
    decide.add_argument("--namespace", nargs="*", help="namespaces the task may touch")
    decide.add_argument("--dialect", default="duckdb")
    decide.add_argument("--json", action="store_true")
    decide.set_defaults(handler=safety_check)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        code = args.handler(args)
    except (OSError, ValueError) as err:
        print(f"error: {err}", file=sys.stderr)
        code = 2
    raise SystemExit(code)


if __name__ == "__main__":
    main()
