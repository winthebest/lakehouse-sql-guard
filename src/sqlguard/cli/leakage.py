"""`sqlguard leakage ...`: temporal leakage in training-set SQL."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from sqlguard.cli import register
from sqlguard.core.catalog import add_catalog_args, catalog_from_args


def _sql_files(target: Path) -> list[Path]:
    if target.is_dir():
        return sorted(target.rglob("*.sql"))
    return [target]


def leakage_check(args: argparse.Namespace) -> int:
    from sqlguard.leakage.replay import Replayer
    from sqlguard.leakage.spec import LeakageSpec
    from sqlguard.leakage.validator import Weights, validate

    spec = LeakageSpec.from_yaml(args.spec)
    catalog = catalog_from_args(args)
    schema = None
    if catalog is None:
        if not args.schema:
            print(
                "error: pass --warehouse or --catalog (Iceberg) or --schema (YAML)",
                file=sys.stderr,
            )
            return 2
        import yaml

        schema = yaml.safe_load(Path(args.schema).read_text(encoding="utf-8"))
    if args.replay and catalog is None:
        print("error: --replay needs --warehouse or --catalog", file=sys.stderr)
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


_COMMANDS = {"check": leakage_check}


def add_parser(subparsers) -> argparse.ArgumentParser:
    leakage = subparsers.add_parser("leakage", help="temporal leakage in training-set SQL")
    leakage_sub = leakage.add_subparsers(dest="command", required=True)
    check = leakage_sub.add_parser("check", help="check .sql files or directories of them")
    check.add_argument("targets", nargs="+", metavar="target")
    check.add_argument("--spec", required=True, help="LeakageSpec YAML")
    add_catalog_args(check)
    check.add_argument("--schema", help="YAML {table: {column: type}} when no warehouse")
    check.add_argument("--replay", action="store_true", help="point-in-time replay")
    check.add_argument("--weights", help="scoring weights YAML")
    check.add_argument(
        "--allow-unverifiable",
        action="store_true",
        help="exit 0 for queries the guard could not fully check (default: fail closed)",
    )
    check.add_argument("--json", action="store_true")
    return leakage


def handle(args: argparse.Namespace) -> int:
    return _COMMANDS[args.command](args)


register("leakage", add_parser, handle)
