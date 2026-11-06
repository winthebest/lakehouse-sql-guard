"""Mutation corpus on the fraud project: inject one textbook leak per mutant, check, report recall.

    uv run python -m bench.realworld.mutate [--date YYYYMMDD]

The operators are pre-registered in `mutation-operators.yaml`. A mutant is a copy of the pinned
project with one model rewritten by an operator's text edits. The rewritten models are written to
`bench/realworld/mutations/` with the project's MIT license and an attribution header. Every
mutant, and the unmutated control, is checked with the project's own spec exactly like the other
projects (`bench.realworld.run`).

The recall reported is recall BY CONSTRUCTION: the operators mirror the rules.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
from collections import Counter
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from bench.realworld import rows
from bench.realworld.checkout import ROOT, WORK, Project, load_projects
from bench.realworld.run import PROJECTS_FILE, RESULTS, check_project, git_short_sha, load_spec

OPERATORS_FILE = ROOT / "bench" / "realworld" / "mutation-operators.yaml"
MUTATIONS = ROOT / "bench" / "realworld" / "mutations"
MUTANT_COLUMNS = (
    "mutant",
    "operator",
    "leak",
    "model",
    "verdict",
    "score",
    "findings",
    "caught_same_rule",
    "caught_new",
    "masked",
    "leaky",
    "right_model",
    "exploratory_evidence_changed",
    "note",
)


@dataclass(frozen=True)
class Mutant:
    operator: str
    leak: str
    model: str

    @property
    def id(self) -> str:
        return f"{self.operator}__{self.model}"


def load_operators(path: Path | str = OPERATORS_FILE) -> dict[str, Any]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    ids = [op["id"] for op in data["operators"]]
    if len(ids) != len(set(ids)):
        raise ValueError("operator ids must be unique")
    return data


def mutants(data: dict[str, Any]) -> list[Mutant]:
    return [
        Mutant(op["id"], op["leak"], model) for op in data["operators"] for model in op["models"]
    ]


def apply_operator(text: str, operator: dict[str, Any]) -> str:
    """The model text with every edit of `operator` applied in order.

    An edit that matches nothing is an error: a mutant must really differ from the original.
    """
    mutated = text
    for edit in operator["edits"]:
        mutated, count = re.subn(edit["find"], edit["replace"], mutated)
        if count == 0:
            raise ValueError(f"{operator['id']}: edit {edit['find']!r} matches nothing")
    if mutated == text:
        raise ValueError(f"{operator['id']}: the edits leave the model unchanged")
    return mutated


def attribution_header(operator: dict[str, Any], data: dict[str, Any], sha: str, path: str) -> str:
    return (
        f"-- Derived from {data['base']} ({data['license']}, see LICENSE in this directory),\n"
        f"-- file {path} at commit {sha[:12]}.\n"
        f"-- Mutated by operator {operator['id']} ({operator['leak']}):\n"
        f"-- {operator['changes']}. This file is deliberately leaky.\n"
    )


def find_model_file(dbt_dir: Path, model: str) -> Path:
    found = [p for p in (dbt_dir / "models").rglob(f"{model}.sql")]
    if len(found) != 1:
        raise ValueError(f"{model}: expected one model file under {dbt_dir}, found {found}")
    return found[0]


def generate(project: Project, data: dict[str, Any]) -> dict[str, str]:
    """Write every mutated model under `mutations/`; returns mutant id -> mutated text."""
    MUTATIONS.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(project.clone / "LICENSE", MUTATIONS / "LICENSE")
    operators = {op["id"]: op for op in data["operators"]}
    texts = {}
    for mutant in mutants(data):
        operator = operators[mutant.operator]
        source = find_model_file(project.dbt_dir, mutant.model)
        relative = source.relative_to(project.clone).as_posix()
        text = apply_operator(source.read_text(encoding="utf-8"), operator)
        text = attribution_header(operator, data, project.sha, relative) + text
        target = MUTATIONS / mutant.operator / f"{mutant.model}.sql"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        texts[mutant.id] = text
    return texts


def _keys(found: list[dict[str, Any]]) -> Counter:
    return Counter((r["rule"], r["model_file"], r["location"]) for r in found)


def score_mutant(
    control: list[dict[str, Any]],
    found: list[dict[str, Any]],
    verdict: str,
    leak: str,
    mutated_file: str,
) -> dict[str, Any]:
    """The pre-registered scores of one mutant against the control's findings."""
    same = [r for r in found if r["rule"] == leak]
    new = _keys(same) - _keys([r for r in control if r["rule"] == leak])
    control_evidence = {r["evidence"] for r in control if r["rule"] == leak}
    return {
        "caught_same_rule": bool(same),
        "caught_new": bool(new),
        "masked": bool(same) and not new,
        "leaky": verdict == "leaky",
        "right_model": any(mutated_file in r["model_file"] for r in same),
        # not pre-registered: the finding's evidence (window, predicates) differs from the control's
        "exploratory_evidence_changed": any(r["evidence"] not in control_evidence for r in same),
    }


def recall(results: list[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    """Per `key` value (operator or leak): mutants and how many each score caught."""
    table: dict[str, dict[str, Any]] = {}
    for result in results:
        entry = table.setdefault(
            result[key],
            {"mutants": 0, "caught_same_rule": 0, "caught_new": 0, "leaky": 0, "right_model": 0},
        )
        entry["mutants"] += 1
        for column in ("caught_same_rule", "caught_new", "leaky", "right_model"):
            entry[column] += int(bool(result[column]))
    for entry in table.values():
        entry["recall_same_rule"] = round(entry["caught_same_rule"] / entry["mutants"], 3)
        entry["recall_new"] = round(entry["caught_new"] / entry["mutants"], 3)
    return table


def variant_project(base: Project, name: str) -> Project:
    return Project(
        repo=base.repo,
        group=base.group,
        sha=base.sha,
        license=base.license,
        publish=base.publish,
        dbt_subdir=base.dbt_subdir,
        default_branch=base.default_branch,
        variant=name,
    )


def make_copy(base: Project, variant: Project) -> None:
    if variant.clone.exists():
        shutil.rmtree(variant.clone)
    shutil.copytree(
        base.clone,
        variant.clone,
        ignore=shutil.ignore_patterns(".git", "build_*", "logs", "target"),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="bench.realworld.mutate", description=__doc__.splitlines()[0]
    )
    parser.add_argument("--date", default=date.today().strftime("%Y%m%d"))
    parser.add_argument("--out", type=Path, default=RESULTS)
    args = parser.parse_args(argv)

    rows.assert_rules_frozen(ROOT)
    data = load_operators()
    base = next(p for p in load_projects(PROJECTS_FILE) if p.repo == data["base"])
    if not base.clone.is_dir():
        raise SystemExit(f"run `python -m bench.realworld.run --projects {base.repo}` first")
    spec = load_spec(base)
    texts = generate(base, data)
    operators = {op["id"]: op for op in data["operators"]}

    control_record, control_rows, _ = check_project(base, spec, prepared=True)
    control_model = control_record["models"][0]
    print(
        f"control: {control_model['verdict']}, {len(control_rows)} findings "
        f"({sorted({r['rule'] for r in control_rows})})"
    )

    results = []
    for mutant in mutants(data):
        variant = variant_project(base, mutant.id.replace("-", "_"))
        make_copy(base, variant)
        source = find_model_file(variant.dbt_dir, mutant.model)
        source.write_text(texts[mutant.id], encoding="utf-8")
        relative = source.relative_to(variant.dbt_dir).as_posix()
        note = ""
        try:
            record, found, _ = check_project(variant, spec, prepared=True)
            model = record["models"][0]
        except Exception as err:  # noqa: BLE001 - a mutant that cannot be built counts as missed
            record, found, model, note = {}, [], {"verdict": "error", "findings": 0}, str(err)[:300]
        verdict = model["verdict"]
        scores = score_mutant(control_rows, found, verdict, mutant.leak, relative)
        results.append(
            {
                "mutant": mutant.id,
                "operator": mutant.operator,
                "leak": mutant.leak,
                "model": mutant.model,
                "verdict": verdict,
                "score": model.get("score", ""),
                "findings": len(found),
                **scores,
                "note": note or model.get("error", ""),
            }
        )
        shutil.rmtree(variant.clone, ignore_errors=True)
        shutil.rmtree(WORK / "profiles" / variant.slug, ignore_errors=True)
        print(
            f"{mutant.id:55} {verdict:13} same_rule={scores['caught_same_rule']!s:5} "
            f"new={scores['caught_new']!s:5} masked={scores['masked']}"
        )

    summary = {
        "by_operator": recall(results, "operator"),
        "by_leak": recall(results, "leak"),
        "no_operator": ["L4"],
        "operators": {op_id: op["changes"] for op_id, op in operators.items()},
    }
    sha = git_short_sha()
    output = {
        "run_date": args.date,
        "code_sha": sha,
        "rules_tag": rows.FROZEN_TAG,
        "base": data["base"],
        "base_sha": base.sha,
        "note": "recall is by construction: the operators mirror the rules",
        "control": {
            "verdict": control_model["verdict"],
            "findings": len(control_rows),
            "rules": dict(Counter(r["rule"] for r in control_rows)),
        },
        "summary": summary,
        "mutants": results,
    }
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / f"mutations-{args.date}-{sha}.json").write_text(
        json.dumps(output, indent=2, default=str) + "\n", encoding="utf-8"
    )
    rows.write_csv(args.out / "mutations.csv", results, MUTANT_COLUMNS)
    print(json.dumps({"control": output["control"], **summary["by_leak"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
