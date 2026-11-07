"""Record what the checker says about every committed spec and split, as a regression fixture.

    uv run python -m bench.leakage.snapshot [--seed 42] [--warehouse .warehouse/bench]
        [--skip-realworld] [--out tests/bench/data/v3-baseline-verdicts.json]

The fixture holds, per case, the verdict, the score (4 decimals) and the sorted rule ids of the
static check and of the combined check (static + point-in-time replay), for every template split
that `splits.yaml` lists and for the Online Retail II cases; and, per dbt training model of the
two real-world rounds, the verdict, score, leak types and the findings (rule, location, model
file, attribution). Timings, dates and confidence intervals are left out so that running it again
on the same code writes the same bytes.

The real-world part compiles the pinned projects with dbt (`bench.realworld.run`) into a
throwaway directory; nothing under `results/` or `.realworld/adjudication*/` is touched.
"""

from __future__ import annotations

import argparse
import csv
import json
import tempfile
from pathlib import Path
from typing import Any

from bench.leakage.cases import LeakCase, generate_cases, load_templates, render
from bench.leakage.freeze import ROOT
from bench.leakage.splits import load_splits
from bench.leakage.world import synthetic_world
from sqlguard.core.catalog import LakehouseCatalog
from sqlguard.leakage.replay import Replayer
from sqlguard.leakage.spec import LeakageSpec
from sqlguard.leakage.validator import LeakageReport, Weights, validate

DEFAULT_OUT = ROOT / "tests" / "bench" / "data" / "v3-baseline-verdicts.json"
REAL_SPLIT = "real"
MAX_POINTS = 24
# (name, projects file, specs dir) of the real-world rounds, relative to the repository root
ROUNDS = (
    ("round1", "bench/realworld/projects.yaml", "bench/realworld/specs"),
    ("round2", "bench/realworld/round2/projects.yaml", "bench/realworld/round2/specs"),
)


def _reading(report: LeakageReport) -> list[Any]:
    return [report.verdict, round(report.score, 4), sorted({f.rule_id for f in report.findings})]


def score_split(
    cases: list[LeakCase], catalog: LakehouseCatalog, spec: LeakageSpec, seed: int
) -> dict[str, dict[str, list[Any]]]:
    """`{case_id: {"static": [...], "combined": [...]}}` for `cases`."""
    schema = catalog.schema_for()
    replayer = Replayer(catalog, spec, max_points=MAX_POINTS, seed=seed)
    weights = Weights.load()
    found = {}
    for case in sorted(cases, key=lambda c: c.case_id):
        static = validate(case.sql, spec, schema=schema, weights=weights)
        combined = validate(case.sql, spec, schema=schema, replay=replayer, weights=weights)
        found[case.case_id] = {"static": _reading(static), "combined": _reading(combined)}
    return found


def leakage_verdicts(seed: int, warehouse: Path) -> dict[str, dict[str, dict[str, list[Any]]]]:
    """Every split of `splits.yaml`, each checked with the spec the manifest gives it, then the
    Online Retail II cases with their own spec."""
    from bench.leakage import real_data

    catalog = synthetic_world(warehouse, seed)
    verdicts = {}
    for name, split in load_splits().items():
        cases = generate_cases(seed, (name,))
        verdicts[name] = score_split(cases, catalog, LeakageSpec.from_yaml(split.spec), seed)
    real_cases = [c for t in load_templates(REAL_SPLIT) for c in render(t)]
    real_catalog = LakehouseCatalog.local(warehouse / "online-retail-ii")
    if real_cases and not real_catalog.table_names():
        real_data.build_real_world(real_catalog)
    verdicts[REAL_SPLIT] = score_split(
        real_cases, real_catalog, LeakageSpec.from_yaml(real_data.SPEC_PATH), seed
    )
    return verdicts


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with open(path, encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _finding(row: dict[str, str]) -> list[str]:
    return [row["rule"], row["location"], row["model_file"], row["attribution"]]


def realworld_verdicts(out_dir: Path) -> dict[str, Any]:
    """Per project and training model, from the files `bench.realworld.run` wrote to `out_dir`."""
    summaries = sorted(out_dir.glob("*.json"))
    if len(summaries) != 1:
        raise ValueError(f"{out_dir}: expected exactly one run summary, found {len(summaries)}")
    records = json.loads(summaries[0].read_text(encoding="utf-8"))["projects"]
    leaks = _read_csv(out_dir / "findings.csv")
    unchecked = _read_csv(out_dir / "unverifiable.csv")
    projects: dict[str, Any] = {}
    for record in sorted(records, key=lambda r: r["repo"]):
        if record["status"] == "not_applicable":
            projects[record["repo"]] = {"status": "not_applicable"}
            continue
        if record["status"] != "checked":
            # a project that could not be fetched or built is not a verdict: record nothing partial
            raise ValueError(f"{record['repo']}: {record['status']}: {record.get('error', '')}")
        models = {}
        for model in sorted(record["models"], key=lambda m: m["model"]):
            name = model["model"]
            entry: dict[str, Any] = {"verdict": model["verdict"]}
            if model["verdict"] == "error":
                lines = [line for line in str(model.get("error", "")).splitlines() if line.strip()]
                entry["error"] = lines[-1].strip() if lines else ""
            else:
                entry["score"] = round(float(model["score"]), 4)
                entry["leak_types"] = sorted(model["leak_types"])
            mine = [r for r in leaks if r["project"] == record["repo"]]
            entry["findings"] = sorted(_finding(r) for r in mine if r["training_model"] == name)
            skipped = [r for r in unchecked if r["project"] == record["repo"]]
            entry["unchecked"] = sorted(_finding(r) for r in skipped if r["training_model"] == name)
            models[name] = entry
        projects[record["repo"]] = {"status": "checked", "models": models}
    return projects


def run_realworld(name: str, projects_file: str, specs_dir: str) -> dict[str, Any]:
    """Check every project of a round with the specs committed for it."""
    from bench.realworld import run as realworld_run

    with tempfile.TemporaryDirectory(prefix=f"sqlguard-{name}-") as scratch:
        out = Path(scratch) / "out"
        realworld_run.main(
            [
                "--projects-file",
                str(ROOT / projects_file),
                "--specs-dir",
                str(ROOT / specs_dir),
                "--out",
                str(out),
                "--full-csv",
                str(Path(scratch) / "full.csv"),
                "--date",
                "00000000",
            ]
        )
        return realworld_verdicts(out)


def snapshot(seed: int, warehouse: Path, with_realworld: bool = True) -> dict[str, Any]:
    from bench.realworld.rows import FROZEN_TAG

    result: dict[str, Any] = {
        "rules_tag": FROZEN_TAG,
        "seed": seed,
        "leakage": leakage_verdicts(seed, warehouse),
    }
    if with_realworld:
        result["realworld"] = {name: run_realworld(name, p, s) for name, p, s in ROUNDS}
    return result


def dumps(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=1, sort_keys=True, ensure_ascii=True) + "\n"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m bench.leakage.snapshot", description=__doc__)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--warehouse", type=Path, default=Path(".warehouse/bench"))
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument(
        "--skip-realworld", action="store_true", help="only the template splits (no dbt, no clones)"
    )
    args = parser.parse_args(argv)
    data = snapshot(args.seed, args.warehouse, with_realworld=not args.skip_realworld)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(dumps(data), encoding="utf-8")
    cases = sum(len(v) for v in data["leakage"].values())
    print(f"wrote {args.out}: {cases} cases in {len(data['leakage'])} splits")


if __name__ == "__main__":
    main()
