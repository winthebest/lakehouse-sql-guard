"""Put the hand verdicts on a run's findings and write the numbers that go with them.

    uv run python -m bench.realworld.assemble RUN_DIR --old OLD_FINDINGS.csv
        [--judge-a A.csv --judge-b B.csv --judged JUDGED_ROWS.csv] [--out-metrics metrics.json]

`RUN_DIR` holds what `bench.realworld.run` wrote (`findings.csv`, `models.csv`). A finding that an
earlier adjudication (`--old`) already judged keeps that verdict (matched by project, model, rule,
location and message, not by number). The findings of the two blind judges (`--judge-a`,
`--judge-b`, each `id,label,reason`, for the rows listed in `--judged`) are matched the same way
and merged: they agree, that is the final label; they disagree, it is `unsure`. A finding nobody
judged keeps empty cells and is counted as `unlabelled`.

The metrics file has the strict and loose precision, the effective false-positive rate (false and
true-but-not-actionable over findings), verdict coverage, findings per model, and the agreement of
the two judges (observed, Cohen's kappa, Gwet's AC1), and of the earlier controller/reviewer pair.
Findings demoted to INFO are in `info-non-feature.csv` and are not counted. All judges are Claude
models; no human rated anything.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from bench.realworld import agreement, rows


def judgments_by_key(
    judged: list[dict[str, Any]], verdicts: dict[str, dict[str, str]]
) -> dict[tuple, list[dict[str, str]]]:
    """`finding key -> verdicts in the order the judged rows list them`."""
    found: dict[tuple, list[dict[str, str]]] = defaultdict(list)
    for row in judged:
        if row["id"] in verdicts:
            found[agreement.finding_key(row)].append(verdicts[row["id"]])
    return found


def assemble(
    findings: list[dict[str, Any]],
    old: list[dict[str, Any]],
    judged: list[dict[str, Any]],
    judge_a: dict[str, dict[str, str]],
    judge_b: dict[str, dict[str, str]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(findings with their verdicts, the rows of the two judges: id, a, reason_a, b, reason_b)."""
    carried, fresh = agreement.carry_verdicts(findings, old)
    by_id = {r["id"]: r for r in carried}
    first = judgments_by_key(judged, judge_a)
    second = judgments_by_key(judged, judge_b)
    seen: dict[tuple, int] = defaultdict(int)
    both: list[dict[str, Any]] = []
    merged_fresh: list[dict[str, Any]] = []
    for row in fresh:
        key = agreement.finding_key(row)
        n = seen[key]
        seen[key] += 1
        if n < len(first.get(key, [])) and n < len(second.get(key, [])):
            a, b = first[key][n], second[key][n]
            merged_fresh += agreement.merge_judgements([row], {row["id"]: a}, {row["id"]: b})
            both.append(
                {
                    "id": row["id"],
                    "reviewer_a": a["label"],
                    "reason_a": a["reason"],
                    "reviewer_b": b["label"],
                    "reason_b": b["reason"],
                }
            )
        else:
            merged_fresh.append(row)
    by_id |= {r["id"]: r for r in merged_fresh}
    return [by_id[r["id"]] for r in findings], both


def metrics(
    findings: list[dict[str, Any]], models: list[dict[str, Any]], new_ids: set[str] = frozenset()
) -> dict[str, Any]:
    """Burden numbers and the agreement of all controller/reviewer pairs, and of the two judges of
    this round alone (`new_ids`)."""
    labelled = [r for r in findings if str(r.get("final") or "").strip()]
    return {
        "dev": True,
        "judges": "Claude models only; no human raters",
        **agreement.burden(findings, models),
        "agreement_all_pairs": agreement.agreement(findings),
        "agreement_this_round": agreement.agreement([r for r in findings if r["id"] in new_ids]),
        "labelled": len(labelled),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--old", type=Path, required=True)
    parser.add_argument("--judged", type=Path)
    parser.add_argument("--judge-a", type=Path)
    parser.add_argument("--judge-b", type=Path)
    parser.add_argument("--verdicts-out", type=Path, help="where to write the two judges' rows")
    parser.add_argument("--out-metrics", type=Path)
    args = parser.parse_args(argv)

    findings = agreement.read_csv(args.run_dir / "findings.csv")
    models = agreement.read_csv(args.run_dir / "models.csv")
    old = agreement.read_csv(args.old)
    judged: list[dict[str, Any]] = agreement.read_csv(args.judged) if args.judged else []
    a = {r["id"]: r for r in agreement.read_csv(args.judge_a)} if args.judge_a else {}
    b = {r["id"]: r for r in agreement.read_csv(args.judge_b)} if args.judge_b else {}
    done, pairs = assemble(findings, old, judged, a, b)
    rows.write_csv(args.run_dir / "findings.csv", done)
    if args.verdicts_out:
        columns = ["id", "reviewer_a", "reason_a", "reviewer_b", "reason_b"]
        rows.write_csv(args.verdicts_out, pairs, columns)
    result = metrics(done, models, {p["id"] for p in pairs})
    text = json.dumps(result, indent=2) + "\n"
    if args.out_metrics:
        args.out_metrics.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
