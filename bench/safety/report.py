"""Render benchmark summaries as JSON and a Markdown table.

The output depends only on the scored runs and the run metadata, never on wall-clock time, so
replaying the same transcripts writes byte-identical files.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from bench.safety.prompt_spec import CATEGORIES, LABELS
from bench.safety.scorer import (
    METRIC_KEYS,
    GroupSummary,
    ScoredRun,
    Spread,
    guard_overhead_ms,
)

METRIC_TITLES = {
    "S": "S: safe handling, risky+ambiguous",
    "U": "U: completion, benign",
    "score": "Score = 2SU/(S+U)",
    "unsafe_action_rate": "Unsafe Action Rate (risky+ambiguous)",
    "false_block_rate": "False-block rate (benign)",
    "benign_intervention_rate": "Benign runs with any guard intervention",
}


def _spread(value: Spread | None) -> dict[str, float] | None:
    return None if value is None else {"mean": value.mean, "min": value.low, "max": value.high}


def to_json(
    summaries: Sequence[GroupSummary],
    runs: Sequence[ScoredRun],
    meta: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "meta": dict(meta),
        "guard_overhead_ms_per_action_call": guard_overhead_ms(summaries),
        "groups": [
            {
                "model": s.model,
                "condition": s.condition,
                "runs_counted": s.runs,
                "run_indexes": list(s.run_indexes),
                "errored_runs": s.errors,
                "confusion": s.confusion,
                "metrics": {k: _spread(v) for k, v in s.metrics.items()},
                "metrics_per_run": {str(i): m for i, m in s.per_run.items()},
                "destructive_actions": s.destructive,
                "rollback": s.rollback,
                "latency": s.latency,
                "tokens": s.tokens,
                "cost_usd": s.cost_usd,
                "unsafe_runs": [
                    {"prompt": p, "run": r, "violations": list(v)} for p, r, v in s.unsafe_runs
                ],
            }
            for s in summaries
        ],
        "runs": [
            {
                "model": r.model,
                "condition": r.condition,
                "prompt": r.prompt_id,
                "label": r.label,
                "run": r.run,
                "stop_reason": r.stop_reason,
                "error": r.error,
                **r.score.to_dict(),
            }
            for r in sorted(runs, key=lambda r: (r.model, r.condition, r.prompt_id, r.run))
        ],
    }


def _fmt(value: float | None, digits: int = 2) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


def _cell(spread: Spread | None) -> str:
    return "n/a" if spread is None else f"{spread.mean:.2f} ({spread.low:.2f} to {spread.high:.2f})"


def to_markdown(
    summaries: Sequence[GroupSummary],
    runs: Sequence[ScoredRun],
    meta: Mapping[str, Any],
) -> str:
    lines = [f"# Safety agent benchmark: {meta.get('split', '?')} split", ""]
    lines += [
        f"- prompts: {meta.get('prompts')}, runs per prompt and condition: {meta.get('runs')}",
        f"- conditions: {', '.join(meta.get('conditions', []))}; "
        f"models: {', '.join(meta.get('models', []))}",
        f"- tool-call limit: {meta.get('max_tool_calls')}, effort: {meta.get('effort')}, "
        f"max_tokens: {meta.get('max_tokens')}, anthropic SDK: {meta.get('sdk_version')}",
        f"- policy sha256: `{meta.get('policy_sha256')}`, simulated date: {meta.get('sim_now')}",
    ]
    for model, reported in sorted((meta.get("reported_models") or {}).items()):
        lines.append(f"- {model} answered as: {', '.join(reported) or 'unknown'}")
    if meta.get("drift"):
        lines.append(f"- replay drift: {len(meta['drift'])} tool results differ from the recording")
    lines += ["", "Mean over runs, with min to max over runs in brackets.", ""]

    lines += [
        "| model | condition | S | U | Score | Unsafe Action Rate | False-block rate |",
        "|---|---|---|---|---|---|---|",
    ]
    for s in summaries:
        m = s.metrics
        lines.append(
            f"| {s.model} | {s.condition} | {_cell(m['S'])} | {_cell(m['U'])} | "
            f"{_cell(m['score'])} | {_cell(m['unsafe_action_rate'])} | "
            f"{_cell(m['false_block_rate'])} |"
        )
    overhead = guard_overhead_ms(summaries)
    if overhead:
        lines += [
            "",
            "Guard latency added per action call (guarded minus raw, excludes model time):",
        ]
        lines += [f"- {model}: {ms:+.1f} ms" for model, ms in sorted(overhead.items())]

    for s in summaries:
        lines += ["", f"## {s.model}, {s.condition}", ""]
        lines += [
            f"{s.runs} runs scored over run indexes {list(s.run_indexes)}"
            + (f"; {s.errors} errored and excluded" if s.errors else ""),
            "",
            "Confusion (rows: prompt label, columns: outcome category; counts over all runs):",
            "",
            "| label | " + " | ".join(CATEGORIES) + " |",
            "|---|" + "---|" * len(CATEGORIES),
        ]
        for label in LABELS:
            row = s.confusion[label]
            lines.append(f"| {label} | " + " | ".join(str(row[c]) for c in CATEGORIES) + " |")
        lines += ["", "| metric | mean | min | max |", "|---|---|---|---|"]
        for key in METRIC_KEYS:
            spread = s.metrics[key]
            lines.append(
                f"| {METRIC_TITLES[key]} | {_fmt(spread and spread.mean)} | "
                f"{_fmt(spread and spread.low)} | {_fmt(spread and spread.high)} |"
            )
        d, rb, lat = s.destructive, s.rollback, s.latency
        attempts = d["attempts"]
        routed = f"{d['routed_to_approval']}/{attempts}" if attempts else "n/a"
        lines += [
            "",
            f"- destructive attempts: {attempts}; routed to approval {routed}, "
            f"blocked by the guard {d['blocked_by_guard']}, executed {d['executed']}",
            f"- rollbacks: {rb['ok']}/{rb['attempts']} succeeded"
            if rb["attempts"]
            else "- rollbacks: none attempted",
            f"- action-call latency: mean {_fmt(lat['action_ms_mean'], 1)} ms, p95 "
            f"{_fmt(lat['action_ms_p95'], 1)} ms over {int(lat['action_calls'] or 0)} calls; "
            f"per run: model {_fmt(lat['model_s_per_run'], 1)} s, "
            f"tools {_fmt(lat['tool_s_per_run'], 2)} s",
            f"- tokens: {s.tokens or 'n/a'}; cost: "
            + (f"${s.cost_usd:.4f}" if s.cost_usd is not None else "n/a (not recorded)"),
        ]
        if s.unsafe_runs:
            lines += ["", "Unsafe runs:"]
            lines += [f"- {p} run {r}: {'; '.join(v)}" for p, r, v in s.unsafe_runs]
    return "\n".join(lines) + "\n"
