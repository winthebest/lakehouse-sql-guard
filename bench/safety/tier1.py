"""Tier-1 safety benchmark: labeled SQL and maintenance actions, no LLM and no network.

Run `uv run python -m bench.safety.tier1` for per-class accuracy, a confusion matrix, the number
of dangerous actions that were allowed, and latency. Exits non-zero on any mismatch.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from sqlguard.core.findings import Decision
from sqlguard.safety import (
    Action,
    DropTable,
    ExpireSnapshots,
    FastForward,
    Policy,
    RemoveOrphanFiles,
    ReversibilityClass,
    RollbackToSnapshot,
    SqlAction,
    TaskScope,
    classify,
)

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CASES = Path(__file__).with_name("tier1_cases.yaml")
DEFAULT_POLICY = ROOT / "config" / "policy.example.yaml"

# Fixed clock so relative ages in the cases are deterministic.
NOW = datetime(2026, 11, 4, 0, 0, tzinfo=UTC)

# Classes whose expected decision is never a plain allow or branch-only write.
SAFE_CLASSES = frozenset({ReversibilityClass.READ, ReversibilityClass.REVERSIBLE_WRITE})


@dataclass(frozen=True)
class Case:
    id: str
    action: Action
    klass: ReversibilityClass
    decision: Decision
    scope: TaskScope | None
    tags: tuple[str, ...]

    @property
    def dangerous(self) -> bool:
        return self.klass not in SAFE_CLASSES


@dataclass(frozen=True)
class CaseResult:
    case: Case
    klass: ReversibilityClass
    decision: Decision
    reasons: tuple[str, ...]
    millis: float

    @property
    def passed(self) -> bool:
        return self.klass == self.case.klass and self.decision == self.case.decision


def load_cases(path: Path = DEFAULT_CASES) -> list[Case]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))["cases"]
    cases = [_build_case(item) for item in raw]
    ids = [c.id for c in cases]
    duplicates = [i for i, n in Counter(ids).items() if n > 1]
    if duplicates:
        raise ValueError(f"duplicate case ids: {duplicates}")
    return cases


def _build_case(item: dict[str, Any]) -> Case:
    if ("sql" in item) == ("action" in item):
        raise ValueError(f"case {item.get('id')!r} needs exactly one of sql or action")
    scope = None
    if "scope" in item:
        scope_cfg = item["scope"] or {}
        scope = TaskScope.of(
            scope_cfg.get("allowed_tables", []), scope_cfg.get("allowed_namespaces", [])
        )
    action = (
        SqlAction(item["sql"], item.get("dialect", "duckdb"))
        if "sql" in item
        else _build_action(item["action"])
    )
    return Case(
        id=item["id"],
        action=action,
        klass=ReversibilityClass(item["klass"]),
        decision=Decision(item["decision"]),
        scope=scope,
        tags=tuple(item.get("tags", ())),
    )


def _build_action(spec: dict[str, Any]) -> Action:
    kind, table = spec["type"], spec["table"]
    days = spec.get("older_than_days_ago")
    older_than = None if days is None else NOW - timedelta(days=days)
    if kind == "expire_snapshots":
        return ExpireSnapshots(
            table,
            older_than=older_than,
            retain_last=spec.get("retain_last"),
            snapshot_ids=tuple(spec.get("snapshot_ids", ())),
        )
    if kind == "remove_orphan_files":
        return RemoveOrphanFiles(table, older_than=older_than)
    if kind == "rollback_to_snapshot":
        return RollbackToSnapshot(table, snapshot_id=spec["snapshot_id"])
    if kind == "fast_forward":
        return FastForward(table, branch=spec["branch"], to=spec["to"])
    if kind == "drop_table":
        return DropTable(table, purge=spec.get("purge", False))
    raise ValueError(f"unknown action type {kind!r}")


def evaluate(cases: list[Case], policy: Policy) -> list[CaseResult]:
    results = []
    for case in cases:
        start = time.perf_counter()
        decision = policy.decide(classify(case.action), case.scope, now=NOW)
        millis = (time.perf_counter() - start) * 1000
        results.append(
            CaseResult(case, decision.klass, decision.decision, decision.reasons, millis)
        )
    return results


def summarize(results: list[CaseResult]) -> dict[str, Any]:
    classes = list(ReversibilityClass)
    per_class = {}
    for klass in classes:
        subset = [r for r in results if r.case.klass == klass]
        per_class[klass.value] = {
            "n": len(subset),
            "class_correct": sum(r.klass == klass for r in subset),
            "decision_correct": sum(r.decision == r.case.decision for r in subset),
        }
    confusion = {
        expected.value: {
            predicted.value: sum(r.case.klass == expected and r.klass == predicted for r in results)
            for predicted in classes
        }
        for expected in classes
    }
    dangerous = [r for r in results if r.case.dangerous]
    safe = [r for r in results if not r.case.dangerous]
    latencies = sorted(r.millis for r in results)
    return {
        "total": len(results),
        "passed": sum(r.passed for r in results),
        "per_class": per_class,
        "confusion": confusion,
        "dangerous_total": len(dangerous),
        "dangerous_allowed": sum(r.decision == Decision.ALLOW for r in dangerous),
        "safe_total": len(safe),
        "safe_blocked": sum(r.decision in (Decision.ASK, Decision.DENY) for r in safe),
        "latency_ms": {
            "median": round(statistics.median(latencies), 3),
            "p95": round(latencies[int(0.95 * (len(latencies) - 1))], 3),
            "max": round(latencies[-1], 3),
        },
        "failures": [
            {
                "id": r.case.id,
                "expected": [r.case.klass.value, r.case.decision.value],
                "actual": [r.klass.value, r.decision.value],
                "reasons": list(r.reasons),
            }
            for r in results
            if not r.passed
        ],
    }


def format_report(summary: dict[str, Any]) -> str:
    lines = [f"tier-1 safety: {summary['passed']}/{summary['total']} cases correct", ""]
    lines.append(f"{'expected class':<18}{'n':>4}{'class acc':>11}{'decision acc':>14}")
    for name, row in summary["per_class"].items():
        if not row["n"]:
            continue
        lines.append(
            f"{name:<18}{row['n']:>4}{row['class_correct'] / row['n']:>11.0%}"
            f"{row['decision_correct'] / row['n']:>14.0%}"
        )
    lines += ["", "confusion (rows expected, columns predicted; non-zero cells):"]
    for expected, row in summary["confusion"].items():
        cells = ", ".join(f"{p}={n}" for p, n in row.items() if n)
        if cells:
            lines.append(f"  {expected:<18} -> {cells}")
    latency = summary["latency_ms"]
    lines += [
        "",
        f"dangerous actions allowed: {summary['dangerous_allowed']}/{summary['dangerous_total']}",
        f"safe actions blocked (ask/deny): {summary['safe_blocked']}/{summary['safe_total']}",
        f"latency ms: median {latency['median']}, p95 {latency['p95']}, max {latency['max']}",
    ]
    for failure in summary["failures"]:
        lines.append(
            f"FAIL {failure['id']}: expected {failure['expected']}, got {failure['actual']}; "
            f"{'; '.join(failure['reasons'])}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Score the tier-1 safety cases.")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--json", action="store_true", help="print the summary as JSON")
    args = parser.parse_args(argv)

    summary = summarize(evaluate(load_cases(args.cases), Policy.load(args.policy)))
    print(json.dumps(summary, indent=2) if args.json else format_report(summary))
    ok = not summary["failures"] and summary["dangerous_allowed"] == 0
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
