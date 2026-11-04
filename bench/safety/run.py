"""Run or replay the safety agent benchmark.

    sqlguard-bench safety agent --model claude-opus-5-5 --condition both --runs 3 --split dev
    sqlguard-bench safety agent --split dev --replay transcripts/safety/<run-dir>

A live run needs Anthropic credentials (`ANTHROPIC_API_KEY` or an `ant auth login` profile).
Main model `claude-opus-5-5`, cheaper comparison `claude-haiku-5-5`. For each model, condition
and run index it plays every prompt against a fresh temporary warehouse and appends the whole
exchange to one JSONL transcript per run.

`--replay` needs no credentials: it re-executes the recorded tool calls against fresh warehouses
and scores the result, so the numbers depend only on the transcripts and the current scorer.

Conditions:
    raw      the agent's tools call the executor directly (full write access to main)
    guarded  every call goes through `Guard.submit`. Approval requests are answered by a scripted
             approver per prompt that approves only the actions listed in the prompt's
             `expected.approve` and rejects everything else.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bench.safety import report
from bench.safety.agent import (
    COMPARISON_MODEL,
    DEFAULT_MAX_TOOL_CALLS,
    EFFORT,
    MAIN_MODEL,
    MAX_TOKENS,
    FatalModelError,
    cost_usd,
    dispatch,
    make_client,
    run_agent,
    sdk_version,
    system_prompt,
)
from bench.safety.fixtures import POLICY_PATH, SIM_NOW, World, capture_state, temp_world
from bench.safety.prompt_spec import (
    Prompt,
    PromptError,
    ScriptedApprover,
    load_prompts,
)
from bench.safety.scorer import RunEvidence, RunScore, ScoredRun, ToolEvent, score_run, summarize
from bench.safety.state import StateSnapshot
from sqlguard.safety.tools import GuardedBackend, RawBackend, ToolBackend

ROOT = Path(__file__).resolve().parents[2]
RESULTS_DIR = ROOT / "results" / "safety"
TRANSCRIPT_DIR = ROOT / "transcripts" / "safety"
CONDITIONS = ("raw", "guarded")
SCHEMA_VERSION = 2


class ReplayError(RuntimeError):
    """A transcript cannot be replayed (unreadable, truncated, or its prompt changed)."""


def make_backend(condition: str, world: World, prompt: Prompt) -> ToolBackend:
    if condition == "raw":
        return RawBackend(world.catalog)
    if condition == "guarded":
        approver = ScriptedApprover(prompt.id, prompt.approve)
        return GuardedBackend(world.guard(), prompt.task_scope.to_scope(), approver)
    raise ValueError(f"unknown condition {condition!r}")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text)


class TranscriptWriter:
    """Appends one JSON object per line, flushing each, so an interrupted run keeps its record."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._file = path.open("a", encoding="utf-8")

    def emit(self, record: dict[str, Any]) -> None:
        self._file.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        self._file.flush()

    def close(self) -> None:
        self._file.close()


@dataclass(frozen=True)
class LiveConfig:
    split: str
    models: tuple[str, ...]
    conditions: tuple[str, ...]
    runs: int
    max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS


def _score(
    prompt: Prompt,
    world: World,
    events: Sequence[ToolEvent],
    final_text: str,
    before: StateSnapshot,
    after: StateSnapshot,
):
    evidence = RunEvidence(tuple(events), final_text, before, after)
    return score_run(prompt, evidence, world.policy.protected_tables)


def execute_run(
    prompt: Prompt,
    condition: str,
    client: Any,
    model: str,
    run_index: int,
    config: LiveConfig,
    transcript: Path,
) -> ScoredRun:
    """One prompt, one condition, one run index, against a fresh warehouse."""
    writer = TranscriptWriter(transcript)
    try:
        with temp_world(prompt.setup) as world:
            before = capture_state(world, prompt.probes())
            backend = make_backend(condition, world, prompt)
            system_text = system_prompt(world.now)
            writer.emit(
                {
                    "type": "run_start",
                    "schema_version": SCHEMA_VERSION,
                    "split": config.split,
                    "prompt_id": prompt.id,
                    "prompt_sha256": _sha256(prompt.text),
                    "condition": condition,
                    "provider": "anthropic",
                    "model": model,
                    "run": run_index,
                    "system_prompt": system_text,
                    "user_text": prompt.text,
                    "sim_now": world.now.isoformat(),
                    "max_tool_calls": config.max_tool_calls,
                    "max_tokens": MAX_TOKENS,
                    "effort": EFFORT,
                    "sdk_version": sdk_version(),
                    "policy_sha256": _sha256(POLICY_PATH.read_text(encoding="utf-8")),
                }
            )
            agent = run_agent(
                client,
                model,
                backend,
                prompt.text,
                system_text,
                writer.emit,
                max_tool_calls=config.max_tool_calls,
            )
            after = capture_state(world, prompt.probes())
            events = [
                ToolEvent.from_record({**result.to_dict(), "elapsed_s": elapsed})
                for result, elapsed in agent.results
            ]
            score = _score(prompt, world, events, agent.final_text, before, after)
            cost = cost_usd(model, agent.usage)
            writer.emit(
                {
                    "type": "run_end",
                    "final_text": agent.final_text,
                    "stop_reason": agent.stop_reason,
                    "api_stop_reason": agent.api_stop_reason,
                    "refusal": agent.refusal,
                    "error": agent.error,
                    "usage": agent.usage,
                    "cost_usd": cost,
                    "model_seconds": agent.model_seconds,
                    "reported_models": agent.reported_models,
                    "score": score.to_dict(),
                    "end_state": after.to_dict(),
                }
            )
    except FatalModelError as err:
        writer.emit({"type": "error", "error": str(err)})
        raise
    finally:
        writer.close()
    return ScoredRun(
        prompt.id,
        prompt.label,
        model,
        condition,
        run_index,
        score,
        tuple(events),
        agent.model_seconds,
        agent.usage,
        cost,
        agent.stop_reason,
        agent.error,
    )


# -- replay --------------------------------------------------------------------------------------


def read_transcript(path: Path) -> list[dict[str, Any]]:
    try:
        lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        records = [json.loads(line) for line in lines]
    except (OSError, json.JSONDecodeError) as err:
        raise ReplayError(f"{path}: unreadable transcript: {err}") from err
    if not records or records[0].get("type") != "run_start":
        raise ReplayError(f"{path}: does not start with a run_start record")
    if records[0].get("schema_version") != SCHEMA_VERSION:
        raise ReplayError(
            f"{path}: unsupported transcript schema {records[0].get('schema_version')}"
        )
    return records


def play(
    prompt: Prompt,
    condition: str,
    calls: Sequence[dict[str, Any]],
    final_text: str,
    source: str = "",
) -> tuple[RunScore, list[ToolEvent], list[str]]:
    """Execute tool calls on a fresh warehouse and score the result.

    Each call is a mapping with `name` and `input` (the tool_use input the model wrote) and,
    optionally, the recorded `elapsed_s`, `status` and `step`. When a recorded `status` is
    present, a call whose replayed status or guard outcome differs is reported as drift.
    """
    drift: list[str] = []
    events: list[ToolEvent] = []
    with temp_world(prompt.setup) as world:
        before = capture_state(world, prompt.probes())
        backend = make_backend(condition, world, prompt)
        for call in calls:
            result = dispatch(backend, call["name"], call["input"])
            event = ToolEvent.from_record(
                {**result.to_dict(), "elapsed_s": call.get("elapsed_s", 0.0)}
            )
            events.append(event)
            recorded = call.get("status")
            recorded_outcome = (call.get("guard") or {}).get("outcome")
            if recorded is not None and (event.status, event.guard_outcome) != (
                recorded,
                recorded_outcome,
            ):
                drift.append(
                    f"{source} step {call.get('step')} {call['name']}: recorded "
                    f"{recorded}/{recorded_outcome}, replayed {event.status}/{event.guard_outcome}"
                )
        after = capture_state(world, prompt.probes())
        score = _score(prompt, world, events, final_text, before, after)
    return score, events, drift


def replay_run(
    records: list[dict[str, Any]], prompts: dict[str, Prompt], source: str
) -> tuple[ScoredRun, list[str], dict[str, Any]]:
    """Re-execute the recorded tool_use calls on a fresh warehouse and score the outcome.

    Returns the scored run, descriptions of any tool call whose result differs from the
    recording, and the run_start record. Latency, tokens and cost come from the recording.
    """
    start = records[0]
    end = next((r for r in reversed(records) if r.get("type") == "run_end"), None)
    prompt = prompts.get(start["prompt_id"])
    if prompt is None:
        raise ReplayError(f"{source}: prompt {start['prompt_id']!r} is not in the prompt file")
    if _sha256(prompt.text) != start["prompt_sha256"]:
        raise ReplayError(f"{source}: the text of prompt {prompt.id} changed since the recording")
    model, condition, run_index = start["model"], start["condition"], int(start["run"])
    if end is None:  # the run died before it finished (crash, interrupt, fatal API error)
        failure = next((r.get("error") for r in records if r.get("type") == "error"), None)
        return (
            _errored(prompt, model, condition, run_index, failure or "transcript has no run_end"),
            [],
            start,
        )

    # The model's own tool_use blocks are the source of truth for what was called.
    blocks = {
        b["id"]: b
        for r in records
        if r.get("type") == "response"
        for b in r["message"].get("content", [])
        if b.get("type") == "tool_use"
    }
    calls = []
    for record in records:
        if record.get("type") != "tool_call" or not record.get("executed"):
            continue
        block = blocks.get(record["call_id"])
        if block is None:
            raise ReplayError(f"{source}: tool call {record['call_id']} has no tool_use block")
        calls.append({**record, "name": block["name"], "input": block["input"]})
    score, events, drift = play(prompt, condition, calls, end.get("final_text") or "", source)
    if end.get("score") and end["score"]["category"] != score.category:
        drift.append(
            f"{source}: recorded category {end['score']['category']}, replayed {score.category}"
        )
    scored = ScoredRun(
        prompt.id,
        prompt.label,
        model,
        condition,
        run_index,
        score,
        tuple(events),
        float(end.get("model_seconds") or 0.0),
        end.get("usage") or {},
        end.get("cost_usd"),
        end.get("stop_reason") or "final",
        end.get("error"),
    )
    return scored, drift, start


def _errored(prompt: Prompt, model: str, condition: str, run_index: int, error: str) -> ScoredRun:
    empty = RunScore("block_model", False, False, None)
    return ScoredRun(
        prompt.id,
        prompt.label,
        model,
        condition,
        run_index,
        empty,
        stop_reason="error",
        error=error,
    )


# -- reporting -----------------------------------------------------------------------------------


def _meta(
    split: str,
    prompts: Sequence[Prompt],
    runs: Sequence[ScoredRun],
    starts: Sequence[dict[str, Any]],
    reported: dict[str, set[str]],
    drift: list[str],
    source: str,
) -> dict[str, Any]:
    first = starts[0] if starts else {}
    return {
        "split": split,
        "source": source,
        "prompts": len(prompts),
        "models": sorted({r.model for r in runs}),
        "conditions": sorted({r.condition for r in runs}),
        "runs": max((r.run for r in runs), default=0),
        "max_tool_calls": first.get("max_tool_calls"),
        "effort": first.get("effort"),
        "max_tokens": first.get("max_tokens"),
        "sdk_version": first.get("sdk_version"),
        "policy_sha256": first.get("policy_sha256"),
        "sim_now": first.get("sim_now", SIM_NOW.isoformat()),
        "reported_models": {m: sorted(v) for m, v in sorted(reported.items())},
        "drift": drift,
    }


def write_results(
    out_dir: Path,
    stem: str,
    runs: Sequence[ScoredRun],
    prompts: Sequence[Prompt],
    starts: Sequence[dict[str, Any]],
    reported: dict[str, set[str]],
    drift: list[str],
    source: str,
    split: str,
) -> tuple[Path, Path, str]:
    summaries = summarize(runs)
    meta = _meta(split, prompts, runs, starts, reported, drift, source)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path, md_path = out_dir / f"{stem}.json", out_dir / f"{stem}.md"
    json_path.write_text(
        json.dumps(report.to_json(summaries, runs, meta), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    markdown = report.to_markdown(summaries, runs, meta)
    md_path.write_text(markdown, encoding="utf-8")
    return json_path, md_path, markdown


def replay(
    transcript_dir: Path, prompts: list[Prompt], split: str, out_dir: Path
) -> tuple[Path, Path, str, list[str]]:
    files = sorted(transcript_dir.rglob("*.jsonl"))
    if not files:
        raise ReplayError(f"no .jsonl transcripts under {transcript_dir}")
    by_id = {p.id: p for p in prompts}
    runs, starts, drift = [], [], []
    reported: dict[str, set[str]] = defaultdict(set)
    for path in files:
        records = read_transcript(path)
        scored, run_drift, start = replay_run(records, by_id, str(path.relative_to(transcript_dir)))
        runs.append(scored)
        starts.append(start)
        drift += run_drift
        for record in records:
            if record.get("type") == "run_end":
                reported[scored.model].update(record.get("reported_models") or ())
    json_path, md_path, markdown = write_results(
        out_dir,
        f"{split}-{transcript_dir.name}-replay",
        runs,
        prompts,
        starts,
        reported,
        drift,
        transcript_dir.name,
        split,
    )
    return json_path, md_path, markdown, drift


# -- live ----------------------------------------------------------------------------------------


def run_live(
    config: LiveConfig,
    prompts: list[Prompt],
    client_factory: Callable[[], Any],
    out_dir: Path,
    transcript_root: Path,
    tag: str,
    log: Callable[[str], None] = print,
) -> tuple[Path, Path, str]:
    runs: list[ScoredRun] = []
    starts: list[dict[str, Any]] = []
    reported: dict[str, set[str]] = defaultdict(set)
    total_cost = 0.0
    total = len(config.models) * len(config.conditions) * config.runs * len(prompts)
    done = 0
    for model in config.models:
        client = client_factory()
        for condition in config.conditions:
            for run_index in range(1, config.runs + 1):
                for prompt in prompts:
                    done += 1
                    path = (
                        transcript_root
                        / tag
                        / _slug(model)
                        / condition
                        / f"{prompt.id}__run{run_index}.jsonl"
                    )
                    path.unlink(missing_ok=True)
                    scored = execute_run(prompt, condition, client, model, run_index, config, path)
                    runs.append(scored)
                    start = read_transcript(path)
                    starts.append(start[0])
                    for record in start:
                        if record.get("type") == "run_end":
                            reported[model].update(record.get("reported_models") or ())
                    total_cost += scored.cost_usd or 0.0
                    log(_progress(done, total, model, condition, scored, total_cost, config))
    return write_results(
        out_dir, tag, runs, prompts, starts, reported, [], str(transcript_root / tag), config.split
    )[0:3]


def _progress(
    done: int,
    total: int,
    model: str,
    condition: str,
    run: ScoredRun,
    total_cost: float,
    config: LiveConfig,
) -> str:
    tokens = run.usage.get("input_tokens", 0) + run.usage.get("output_tokens", 0)
    cost = (
        f"${run.cost_usd:.4f} (total ${total_cost:.4f})"
        if run.cost_usd is not None
        else "cost unknown"
    )
    status = run.error and f"ERROR {run.error[:80]}" or run.score.category
    return (
        f"[{done}/{total}] {model} {condition} {run.prompt_id} run{run.run}: {status}; "
        f"{tokens} tokens, {cost}"
    )


# -- command line --------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sqlguard-bench safety agent", description="Run or replay the safety agent benchmark."
    )
    parser.add_argument(
        "--model",
        action="append",
        help=(
            f"Claude model id (required for a live run; repeat for several). "
            f"Main: {MAIN_MODEL}; cheaper comparison: {COMPARISON_MODEL}"
        ),
    )
    parser.add_argument("--condition", choices=[*CONDITIONS, "both"], default="both")
    parser.add_argument("--runs", type=int, default=3, help="runs per prompt and condition")
    parser.add_argument("--split", default="dev", help="prompt split (dev, holdout) or a YAML path")
    parser.add_argument(
        "--only", action="append", metavar="PROMPT_ID", help="run only these prompts"
    )
    parser.add_argument("--max-tool-calls", type=int, default=DEFAULT_MAX_TOOL_CALLS)
    parser.add_argument(
        "--replay", type=Path, metavar="TRANSCRIPT_DIR", help="re-score; needs no credentials"
    )
    parser.add_argument("--results-dir", type=Path, default=RESULTS_DIR)
    parser.add_argument("--transcripts-dir", type=Path, default=TRANSCRIPT_DIR)
    return parser


def main(
    argv: Sequence[str] | None = None,
    client_factory: Callable[[], Any] | None = None,
) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        prompts = load_prompts(args.split)
    except PromptError as err:
        parser.error(str(err))
    if args.only:
        unknown = sorted(set(args.only) - {p.id for p in prompts})
        if unknown:
            parser.error(f"unknown prompt ids: {unknown}")
        prompts = [p for p in prompts if p.id in set(args.only)]
    split = Path(args.split).stem

    if args.replay is not None:
        try:
            json_path, md_path, markdown, drift = replay(
                args.replay, prompts, split, args.results_dir
            )
        except ReplayError as err:
            print(f"replay failed: {err}", file=sys.stderr)
            return 1
        print(markdown)
        print(f"wrote {json_path} and {md_path}")
        for line in drift:
            print(f"DRIFT {line}", file=sys.stderr)
        return 0

    if not args.model:
        parser.error("--model is required for a live run")
    if args.runs < 1 or args.max_tool_calls < 1:
        parser.error("--runs and --max-tool-calls must be at least 1")
    config = LiveConfig(
        split=split,
        models=tuple(args.model),
        conditions=CONDITIONS if args.condition == "both" else (args.condition,),
        runs=args.runs,
        max_tool_calls=args.max_tool_calls,
    )
    tag = f"{split}-{datetime.now(UTC):%Y%m%dT%H%M%SZ}"
    try:
        json_path, md_path, markdown = run_live(
            config,
            prompts,
            client_factory or make_client,
            args.results_dir,
            args.transcripts_dir,
            tag,
        )
    except FatalModelError as err:
        print(f"stopped: {err}", file=sys.stderr)
        return 2
    print(markdown)
    print(f"wrote {json_path} and {md_path}")
    print(f"transcripts in {args.transcripts_dir / tag}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
