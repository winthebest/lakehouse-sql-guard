"""`sqlguard leakage ...`: temporal leakage in training-set SQL."""

from __future__ import annotations

import argparse
import difflib
import json
import sys
from pathlib import Path

from sqlguard.cli import register
from sqlguard.core.catalog import add_catalog_args, catalog_from_args


def _sql_files(target: Path) -> list[Path]:
    if target.is_dir():
        return sorted(target.rglob("*.sql"))
    return [target]


def _print_fixes(
    path: Path | str, fixes: list, owners: list[str] | None = None, note: str = ""
) -> None:
    """The fixes as a list of what each does, then one unified diff of the whole repair.

    `owners` names, per fix, where it belongs when that is not `path` (a dbt model).
    """
    for i, fix in enumerate(fixes):
        where = f" (in {owners[i]})" if owners else ""
        print(
            f"    fix {fix.rule:3} [{fix.verification.value}; semantics {fix.semantics.value}] "
            f"{fix.location}{where}: {fix.explanation}"
        )
        for caveat in fix.caveats:
            print(f"        caveat: {caveat}")
    rewrites = [f for f in fixes if f.rewrites]
    if not rewrites:
        return
    diff = difflib.unified_diff(
        rewrites[0].before_sql.splitlines(),
        rewrites[-1].after_sql.splitlines(),
        fromfile=f"a/{path}",
        tofile=f"b/{path} (suggested)",
        lineterm="",
    )
    print(f"    suggested change (formatting normalized; the file is not modified{note}):")
    for line in diff:
        print(line)


def leakage_check(args: argparse.Namespace) -> int:
    from sqlguard.leakage.fixes import suggest_fix
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
        sql = path.read_text(encoding="utf-8")
        entry = {"file": str(path), **report.to_dict()}
        fixes = []
        if args.suggest_fix:
            fixes = suggest_fix(
                sql,
                spec,
                report,
                schema=schema,
                catalog=catalog,
                replay=replay,
                weights=weights,
            )
            entry["fixes"] = [fix.to_dict() for fix in fixes]
        results.append(entry)
        failing = report.verdict == "leaky" or (
            report.verdict == "unverifiable" and not args.allow_unverifiable
        )
        worst = max(worst, int(failing))
        if not args.json:
            print(f"{report.verdict.upper():13} {report.score:.2f}  {path}")
            for finding in report.findings:
                where = f" [{finding.location}]" if finding.location else ""
                print(f"    {finding.rule_id:12} {finding.message}{where}")
            _print_fixes(path, fixes)
    if args.json:
        print(json.dumps(results, indent=2, default=str))
    return worst


def leakage_dbt(args: argparse.Namespace) -> int:
    import dataclasses

    import yaml

    from sqlguard.dbt.check import check_model, training_sets
    from sqlguard.dbt.manifest import load_column_types, load_manifest
    from sqlguard.leakage.replay import Replayer
    from sqlguard.leakage.validator import Weights

    manifest = load_manifest(args.manifest)
    override = None
    if args.spec:
        override = yaml.safe_load(Path(args.spec).read_text(encoding="utf-8"))
        if not isinstance(override, dict):
            raise ValueError(f"{args.spec}: expected a mapping at the top level")
        if override.get("dialect"):
            manifest = dataclasses.replace(manifest, dialect=str(override["dialect"]))
    column_types = load_column_types(args.catalog_json) if args.catalog_json else None
    catalog = catalog_from_args(args)
    if args.replay and catalog is None:
        print("error: --replay needs --warehouse or --catalog", file=sys.stderr)
        return 2
    weights = Weights.load(args.weights)
    roots = training_sets(manifest, args.select)

    replayers: list[tuple[object, Replayer]] = []  # one per distinct spec, each with its cache

    def replay_for(spec):
        for known, replayer in replayers:
            if known == spec:
                return replayer
        replayers.append((spec, Replayer(catalog, spec)))
        return replayers[-1][1]

    worst = 0
    results = []
    for root in roots:
        checked = check_model(
            manifest,
            root,
            override=override,
            column_types=column_types,
            catalog=catalog,
            replay_for=replay_for if args.replay else None,
            weights=weights,
            suggest=args.suggest_fix,
        )
        results.append(checked)
        report = checked.report
        failing = report.verdict == "leaky" or (
            report.verdict == "unverifiable" and not args.allow_unverifiable
        )
        worst = max(worst, int(failing))
        for warning in checked.warnings:
            print(f"warning: {root.name}: {warning}", file=sys.stderr)
        if args.json:
            continue
        print(f"{report.verdict.upper():13} {report.score:.2f}  {root.original_file_path}")
        for finding, models in zip(report.findings, checked.finding_models, strict=True):
            where = f" [{finding.location}]" if finding.location else ""
            print(f"    {finding.rule_id:12} {finding.message}{where}")
            print(f"    {'':12} in {', '.join(m.original_file_path for m in models)}")
        if args.suggest_fix:
            _print_fixes(
                checked.diff_file,
                checked.fixes,
                checked.fix_files,
                "; shown against the compiled query with upstream models inlined",
            )
    if args.json:
        print(json.dumps([r.to_dict() for r in results], indent=2, default=str))
    return worst


_COMMANDS = {"check": leakage_check, "dbt": leakage_dbt}


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
    check.add_argument(
        "--suggest-fix",
        action="store_true",
        help="print a point-in-time rewrite for each leak (a unified diff; files are never "
        "modified). Add --replay with a warehouse to have the rewrite replayed.",
    )
    check.add_argument("--json", action="store_true")

    dbt = leakage_sub.add_parser(
        "dbt",
        help="check the training_set models of a dbt project (reads manifest.json)",
        description="Check every model with meta.leakage.role: training_set. The manifest must "
        "come from `dbt compile` (compiled_code present for the training models and everything "
        "they are built from).",
    )
    dbt.add_argument("--manifest", required=True, help="manifest.json from `dbt compile`")
    dbt.add_argument("--catalog-json", help="catalog.json from `dbt docs generate` (column types)")
    dbt.add_argument("--spec", help="LeakageSpec YAML merged over the dbt meta (the file wins)")
    dbt.add_argument(
        "--select",
        nargs="+",
        metavar="PATTERN",
        help="only training_set models whose name, unique_id or file path matches (* and ? work)",
    )
    add_catalog_args(dbt)
    dbt.add_argument("--replay", action="store_true", help="point-in-time replay of the sources")
    dbt.add_argument("--weights", help="scoring weights YAML")
    dbt.add_argument(
        "--allow-unverifiable",
        action="store_true",
        help="exit 0 for queries the guard could not fully check (default: fail closed)",
    )
    dbt.add_argument(
        "--suggest-fix",
        action="store_true",
        help="print a point-in-time rewrite for each leak, attributed to the model that holds "
        "it (a diff of the inlined query; no file is modified)",
    )
    dbt.add_argument("--json", action="store_true")
    return leakage


def handle(args: argparse.Namespace) -> int:
    return _COMMANDS[args.command](args)


register("leakage", add_parser, handle)
