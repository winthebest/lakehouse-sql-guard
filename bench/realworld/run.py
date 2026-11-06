"""Run `sqlguard leakage dbt` on the real-world projects and write the findings for adjudication.

    uv run python -m bench.realworld.run [--projects OWNER/REPO ...] [--date YYYYMMDD]

Per project: clone at the pinned commit, compile with dbt-duckdb into `build_<slug>`, apply the
spec's `realworld:` block to a copy of the manifest, then run the checker exactly as a user would
(`sqlguard leakage dbt --manifest ... --spec ... --catalog-json ... --json`, static, no replay:
these projects have no snapshot history), once per training model so that one unusable model does
not hide the others. The rules must be unchanged since the tag `rules-frozen-v3`.

Writes `results/realworld/<date>-<sha>.json` (counts and findings, no code of unlicensed
projects), `results/realworld/findings.csv`, `unverifiable.csv` and `models.csv` (adjudication
columns empty), and `.realworld/adjudication/findings-full.csv` (git-ignored; adds the code
excerpts of unlicensed projects so a person can judge every finding).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from bench.realworld import rows
from bench.realworld.checkout import (
    ROOT,
    Project,
    compile_project,
    ensure_checkout,
    load_projects,
)

PROJECTS_FILE = ROOT / "bench" / "realworld" / "projects.yaml"
SPECS = ROOT / "bench" / "realworld" / "specs"
RESULTS = ROOT / "results" / "realworld"
FULL_CSV = ROOT / ".realworld" / "adjudication" / "findings-full.csv"
CHECK_TIMEOUT_SECONDS = 900


def load_spec(project: Project) -> dict[str, Any]:
    path = SPECS / f"{project.spec_name}.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("realworld"), dict):
        raise rows.RealworldError(f"{path}: expected a mapping with a `realworld:` block")
    return data


def run_checker(
    manifest: Path, spec: Path, catalog: Path, select: str
) -> tuple[int, list[dict[str, Any]], list[str], str, float]:
    """One user-style invocation: (exit code, JSON entries, warnings, error text, seconds)."""
    command = [
        sys.executable,
        "-m",
        "sqlguard.cli",
        "leakage",
        "dbt",
        "--manifest",
        str(manifest),
        "--spec",
        str(spec),
        "--catalog-json",
        str(catalog),
        "--select",
        select,
        "--json",
    ]
    started = time.perf_counter()
    done = subprocess.run(
        command, capture_output=True, text=True, cwd=ROOT, timeout=CHECK_TIMEOUT_SECONDS
    )
    seconds = time.perf_counter() - started
    warnings = [line for line in done.stderr.splitlines() if line.startswith("warning:")]
    if done.returncode in (0, 1):
        return done.returncode, json.loads(done.stdout), warnings, "", seconds
    return done.returncode, [], warnings, done.stderr.strip()[-600:], seconds


def check_project(
    project: Project, spec: dict[str, Any], prepared: bool = False
) -> tuple[dict[str, Any], list, list]:
    """Result record of one project and its finding rows (full view).

    `prepared`: the project directory already holds the files to check (a mutant), so nothing is
    fetched and `dbt deps` is not repeated.
    """
    block = spec["realworld"]
    record: dict[str, Any] = {
        "repo": project.repo,
        "group": project.group,
        "sha": project.sha,
        "license": project.license,
        "publish": project.publish,
    }
    if block.get("applicable") is False:
        record |= {"status": "not_applicable", "reason": block.get("reason", "")}
        return record, [], []

    if not prepared:
        ensure_checkout(project)
    record["build"] = compile_project(project, deps=not prepared)
    manifest = json.loads(project.manifest.read_text(encoding="utf-8"))
    annotated = rows.annotate_manifest(manifest, block)
    build_dir = project.manifest.parent
    annotated_path = build_dir / "manifest.sqlguard.json"
    annotated_path.write_text(json.dumps(annotated), encoding="utf-8")
    catalog_path = build_dir / "catalog.json"
    catalog_path.write_text(
        json.dumps(rows.catalog_from_schema(annotated, block.get("schema") or {})), encoding="utf-8"
    )

    stop = rows.boundary_models(annotated)
    models_total = sum(1 for n in annotated["nodes"].values() if n["resource_type"] == "model")
    inlined: dict[str, None] = {}
    leaks: list[dict[str, Any]] = []
    unchecked: list[dict[str, Any]] = []
    checked_models = []
    seconds = 0.0
    for name in block["training_sets"]:
        unique_id = rows.resolve_node(annotated, f"model:{name}")
        code, entries, warnings, error, took = run_checker(
            annotated_path, SPECS / f"{project.spec_name}.yaml", catalog_path, name
        )
        seconds += took
        inlined |= dict.fromkeys(rows.upstream_models(annotated, unique_id, stop))
        entry = {
            "model": name,
            "exit_code": code,
            "seconds": round(took, 3),
            "warnings": warnings,
        }
        if error:
            entry |= {"verdict": "error", "error": error, "findings": 0, "unchecked": 0}
        else:
            found, skipped = rows.finding_rows(
                {"repo": project.repo, "group": project.group}, entries, annotated
            )
            leaks += found
            unchecked += skipped
            entry |= {
                "verdict": entries[0]["verdict"],
                "score": entries[0]["score"],
                "leak_types": entries[0]["leak_types"],
                "findings": len(found),
                "unchecked": len(skipped),
                "trusted_models": [m["file"] for m in entries[0].get("trusted_models", [])],
            }
        checked_models.append(entry)
    record |= {
        "status": "checked",
        "models_in_project": models_total,
        "training_models": len(checked_models),
        "models_inlined": len(inlined),
        "seconds": round(seconds, 3),
        "models": checked_models,
        "findings": len(leaks),
        "unverifiable": sum(1 for m in checked_models if m["verdict"] == "unverifiable"),
        "errors": sum(1 for m in checked_models if m["verdict"] == "error"),
    }
    return record, leaks, unchecked


def summarise(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Counts per group (no precision: that needs the hand adjudication)."""
    summary: dict[str, Any] = {}
    for group in ("new", "seen"):
        mine = [r for r in records if r["group"] == group]
        checked = [r for r in mine if r["status"] == "checked"]
        summary[group] = {
            "projects": len(mine),
            "checked": len(checked),
            "not_applicable": sum(1 for r in mine if r["status"] == "not_applicable"),
            "models_in_projects": sum(r["models_in_project"] for r in checked),
            "training_models": sum(r["training_models"] for r in checked),
            "findings": sum(r["findings"] for r in checked),
            "leaky_models": sum(1 for r in checked for m in r["models"] if m["verdict"] == "leaky"),
            "unverifiable_models": sum(r["unverifiable"] for r in checked),
            "error_models": sum(r["errors"] for r in checked),
            "seconds": round(sum(r["seconds"] for r in checked), 3),
        }
    return summary


def git_short_sha() -> str:
    done = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True
    )
    return done.stdout.strip() or "unknown"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="bench.realworld.run", description=__doc__.splitlines()[0]
    )
    parser.add_argument("--projects", nargs="+", metavar="OWNER/REPO", help="only these projects")
    parser.add_argument("--date", default=date.today().strftime("%Y%m%d"))
    parser.add_argument("--out", type=Path, default=RESULTS)
    args = parser.parse_args(argv)

    rows.assert_rules_frozen(ROOT)
    projects = load_projects(PROJECTS_FILE)
    if args.projects:
        unknown = set(args.projects) - {p.repo for p in projects}
        if unknown:
            parser.error(f"not in projects.yaml: {sorted(unknown)}")
        projects = [p for p in projects if p.repo in args.projects]

    records: list[dict[str, Any]] = []
    full_rows: list[dict[str, Any]] = []
    public_rows: list[dict[str, Any]] = []
    unchecked_public: list[dict[str, Any]] = []
    for project in projects:
        spec = load_spec(project)
        record, leaks, unchecked = check_project(project, spec)
        records.append(record)
        full_rows += leaks
        public_rows += [rows.public_row(r, project.publish) for r in leaks]
        unchecked_public += [rows.public_row(r, project.publish) for r in unchecked]
        print(
            f"{project.group:4} {project.repo}: {record['status']}"
            + (
                f", {record['training_models']} training models, {record['findings']} findings, "
                f"{record['unverifiable']} unverifiable, {record['seconds']}s"
                if record["status"] == "checked"
                else ""
            )
        )

    sha = git_short_sha()
    result = {
        "run_date": args.date,
        "code_sha": sha,
        "rules_tag": rows.FROZEN_TAG,
        "mode": "static (no replay: no snapshot history)",
        "summary": summarise(records),
        "projects": records,
    }
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / f"{args.date}-{sha}.json").write_text(
        json.dumps(result, indent=2, default=str) + "\n", encoding="utf-8"
    )
    rows.write_csv(args.out / "findings.csv", public_rows)
    rows.write_csv(args.out / "unverifiable.csv", unchecked_public)
    rows.write_csv(args.out / "models.csv", rows.model_rows(records), rows.MODEL_COLUMNS)
    rows.write_csv(FULL_CSV, full_rows)
    print(json.dumps(result["summary"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
