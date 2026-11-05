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
    guarded-pending
             the flow the MCP server offers. An action that needs approval is rehearsed, saved in
             an action store and reported to the agent as `pending_approval`; the agent may call
             `get_action_status` but cannot decide anything. When the agent ends its turn the
             harness resolves each action it left pending with the same decision table as the
             scripted approver (`Guard.resolve`), then scores the tables. An action that went
             stale because an earlier one changed the table counts as what it is: not applied.

`--policy PATH` picks the policy file for every condition (default `config/policy.example.yaml`);
a replay must be given the policy the transcripts were recorded with. `--pilot` plays one prompt
of each label once and prints what the whole run would cost, without running it; `--budget USD`
stops a live run as soon as its cost passes the limit.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
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
    LABELS,
    Prompt,
    PromptError,
    ScriptedApprover,
    load_prompts,
)
from bench.safety.scorer import RunEvidence, RunScore, ScoredRun, ToolEvent, score_run, summarize
from bench.safety.state import StateSnapshot
from sqlguard.safety import (
    ActionStore,
    Policy,
    PolicyError,
    ProofRequest,
    SqlAction,
    classify,
)
from sqlguard.safety.guard import PendingActionError
from sqlguard.safety.store import ActionRecord, StoreError, decode_action
from sqlguard.safety.tools import (
    GuardedBackend,
    RawBackend,
    ToolBackend,
    ToolResult,
    action_to_dict,
)

ROOT = Path(__file__).resolve().parents[2]
RESULTS_DIR = ROOT / "results" / "safety"
TRANSCRIPT_DIR = ROOT / "transcripts" / "safety"
CONDITIONS = ("raw", "guarded", "guarded-pending")
DEFAULT_CONDITIONS = ("raw", "guarded")  # what `--condition both` means
PENDING = "guarded-pending"
SCHEMA_VERSION = 2


class ReplayError(RuntimeError):
    """A transcript cannot be replayed (unreadable, truncated, or its prompt changed)."""


def make_backend(condition: str, world: World, prompt: Prompt) -> ToolBackend:
    if condition == "raw":
        return RawBackend(world.catalog)
    if condition == "guarded":
        approver = ScriptedApprover(prompt.id, prompt.approve)
        return GuardedBackend(world.guard(), prompt.task_scope.to_scope(), approver)
    if condition == PENDING:
        # as in the MCP server: a store, a fixed requester, no approver and no proof verifier
        guard = world.guard(store=_store(world), requester=_AGENT)
        return GuardedBackend(guard, prompt.task_scope.to_scope())
    raise ValueError(f"unknown condition {condition!r}")


_AGENT = "bench-agent"


def _store(world: World) -> ActionStore:
    return ActionStore(world.root / "actions.db")


class _ScriptedProofs:
    """Accepts exactly the proof the harness mints for its own scripted decision."""

    @staticmethod
    def proof(action_id: str, decision: str) -> str:
        return f"scripted:{action_id}:{decision}"

    def verify(self, request: ProofRequest, proof: str) -> bool:
        return proof == self.proof(request.action_id, request.decision)


@dataclass(frozen=True)
class Resolution:
    """How the harness settled one action the agent left pending."""

    action_id: str
    decision: str  # approve | reject
    outcome: str  # the guard outcome, or why it could not be decided
    action: dict[str, Any]
    reasons: tuple[str, ...] = ()

    def to_record(self) -> dict[str, Any]:
        return {
            "type": "resolution",
            "action_id": self.action_id,
            "decision": self.decision,
            "outcome": self.outcome,
            "action": self.action,
            "reasons": list(self.reasons),
        }


def settle_pending(
    condition: str, world: World, prompt: Prompt, results: Sequence[ToolResult]
) -> list[Resolution]:
    """Decide the actions the agent left pending, in the order it submitted them.

    The decision is the scripted approver's: approve exactly what `expected.approve` lists,
    reject the rest. Nothing is decided for any other condition.
    """
    if condition != PENDING:
        return []
    store = _store(world)
    approver = ScriptedApprover(prompt.id, prompt.approve)
    reviewer = world.guard(store=store, proof_verifier=_ScriptedProofs(), requester=_AGENT)
    resolutions: list[Resolution] = []
    seen: set[str] = set()
    for result in results:
        action_id = (result.guard or {}).get("action_id")
        if result.status != "pending_approval" or not action_id or action_id in seen:
            continue
        seen.add(action_id)
        record = store.get(action_id)
        if record is None:
            resolutions.append(Resolution(action_id, "reject", "missing", {}))
            continue
        action = _action_of(record)
        tables = (
            (record.table,)
            if record.maintenance_json
            else classify(action, world.catalog).write_tables
        )
        decision = "approve" if approver.decides(action, tables) else "reject"
        try:
            decided = reviewer.resolve(
                action_id,
                decision,  # type: ignore[arg-type]
                approver.approver_id,
                _ScriptedProofs.proof(action_id, decision),
            )
            outcome, reasons = decided.outcome.value, tuple(decided.reasons)
        except (PendingActionError, StoreError) as err:
            outcome, reasons = type(err).__name__, (str(err),)
        resolutions.append(
            Resolution(action_id, decision, outcome, action_to_dict(action), reasons)
        )
    return resolutions


def _action_of(record: ActionRecord):
    maintenance = record.maintenance_data
    if maintenance is not None:
        return decode_action(maintenance["action"])
    return SqlAction(record.sql_normalized or "", record.dialect)


def events_with_resolutions(
    results: Sequence[tuple[ToolResult, float]], resolutions: Sequence[Resolution]
) -> list[ToolEvent]:
    """The run's tool events; a call that left an action pending carries how it was settled.

    The settlement goes where an approver's answer would: `approvals`, so the scorer counts a
    scripted approval or rejection the same way in both approval conditions.
    """
    by_id = {r.action_id: r for r in resolutions}
    events = []
    for result, elapsed in results:
        record = {**result.to_dict(), "elapsed_s": elapsed}
        resolution = by_id.get((result.guard or {}).get("action_id") or "")
        if resolution is not None and result.status == "pending_approval":
            record["approvals"] = [
                {
                    "action": resolution.action,
                    "approved": resolution.decision == "approve",
                    "resolved_as": resolution.outcome,
                }
            ]
        events.append(ToolEvent.from_record(record))
    return events


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
    policy: Path = POLICY_PATH
    budget_usd: float | None = None  # stop the run once its cost reaches this


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
        with temp_world(prompt.setup, config.policy) as world:
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
                    "policy_sha256": _sha256(config.policy.read_text(encoding="utf-8")),
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
            resolutions = settle_pending(
                condition, world, prompt, [result for result, _ in agent.results]
            )
            for resolution in resolutions:
                writer.emit(resolution.to_record())
            after = capture_state(world, prompt.probes())
            events = events_with_resolutions(agent.results, resolutions)
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
    policy: Path | str = POLICY_PATH,
) -> tuple[RunScore, list[ToolEvent], list[str]]:
    """Execute tool calls on a fresh warehouse and score the result.

    Each call is a mapping with `name` and `input` (the tool_use input the model wrote) and,
    optionally, the recorded `elapsed_s`, `status` and `step`. When a recorded `status` is
    present, a call whose replayed status or guard outcome differs is reported as drift.
    """
    drift: list[str] = []
    results: list[tuple[ToolResult, float]] = []
    ids: dict[str, str] = {}  # action id in the recording -> action id in this replay
    with temp_world(prompt.setup, policy) as world:
        before = capture_state(world, prompt.probes())
        backend = make_backend(condition, world, prompt)
        for call in calls:
            arguments = call["input"]
            if call["name"] == "get_action_status" and isinstance(arguments, dict):
                arguments = {**arguments, "action_id": ids.get(arguments.get("action_id"), "")}
            result = dispatch(backend, call["name"], arguments)
            results.append((result, call.get("elapsed_s", 0.0)))
            recorded_id = (call.get("result") or {}).get("action_id")
            if recorded_id and (result.guard or {}).get("action_id"):
                ids[recorded_id] = result.guard["action_id"]
            event = ToolEvent.from_record(
                {**result.to_dict(), "elapsed_s": call.get("elapsed_s", 0.0)}
            )
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
        resolutions = settle_pending(condition, world, prompt, [r for r, _ in results])
        after = capture_state(world, prompt.probes())
        events = events_with_resolutions(results, resolutions)
        score = _score(prompt, world, events, final_text, before, after)
    return score, events, drift


def replay_run(
    records: list[dict[str, Any]],
    prompts: dict[str, Prompt],
    source: str,
    policy: Path = POLICY_PATH,
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
    score, events, drift = play(
        prompt, condition, calls, end.get("final_text") or "", source, policy
    )
    recorded_policy = start.get("policy_sha256")
    if recorded_policy and recorded_policy != _sha256(policy.read_text(encoding="utf-8")):
        drift.append(f"{source}: replayed under a policy other than the one recorded")
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
    transcript_dir: Path,
    prompts: list[Prompt],
    split: str,
    out_dir: Path,
    policy: Path = POLICY_PATH,
) -> tuple[Path, Path, str, list[str]]:
    files = sorted(transcript_dir.rglob("*.jsonl"))
    if not files:
        raise ReplayError(f"no .jsonl transcripts under {transcript_dir}")
    by_id = {p.id: p for p in prompts}
    runs, starts, drift = [], [], []
    reported: dict[str, set[str]] = defaultdict(set)
    for path in files:
        records = read_transcript(path)
        scored, run_drift, start = replay_run(
            records, by_id, str(path.relative_to(transcript_dir)), policy
        )
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


def _shown(path: Path) -> str:
    """`path` for a results file: relative to the repository, or just its name; never absolute."""
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return path.name


class BudgetExceeded(RuntimeError):
    """A live run reached its `--budget`; what was played so far is reported."""


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
    try:
        for model in config.models:
            client = client_factory()
            for condition in config.conditions:
                for run_index in range(1, config.runs + 1):
                    for prompt in prompts:
                        if config.budget_usd is not None and total_cost >= config.budget_usd:
                            raise BudgetExceeded(
                                f"spent ${total_cost:.4f} of the ${config.budget_usd:.2f} budget "
                                f"after {done} of {total} runs"
                            )
                        done += 1
                        path = (
                            transcript_root
                            / tag
                            / _slug(model)
                            / condition
                            / f"{prompt.id}__run{run_index}.jsonl"
                        )
                        path.unlink(missing_ok=True)
                        scored = execute_run(
                            prompt, condition, client, model, run_index, config, path
                        )
                        runs.append(scored)
                        start = read_transcript(path)
                        starts.append(start[0])
                        for record in start:
                            if record.get("type") == "run_end":
                                reported[model].update(record.get("reported_models") or ())
                        total_cost += scored.cost_usd or 0.0
                        log(_progress(done, total, model, condition, scored, total_cost, config))
    except BudgetExceeded as err:
        log(f"STOPPED: {err}")
    return write_results(
        out_dir,
        tag,
        runs,
        prompts,
        starts,
        reported,
        [],
        _shown(transcript_root / tag),
        config.split,
    )[0:3]


# -- cost pilot ----------------------------------------------------------------------------------


def pilot_sample(prompts: Sequence[Prompt]) -> list[Prompt]:
    """The first prompt of each label: a benign, a risky and an ambiguous one."""
    first: dict[str, Prompt] = {}
    for prompt in prompts:
        first.setdefault(prompt.label, prompt)
    return [first[label] for label in LABELS if label in first]


def run_pilot(
    config: LiveConfig,
    prompts: list[Prompt],
    client_factory: Callable[[], Any],
    transcript_root: Path,
    tag: str,
    log: Callable[[str], None] = print,
) -> dict[str, Any]:
    """Play one run of a small sample, then say what the whole planned run would cost.

    The estimate is the measured mean cost of a run per model and condition times the number of
    runs planned (prompts x runs). It is an extrapolation from a few runs, not a bound.
    """
    sample = pilot_sample(prompts)
    one = replace(config, runs=1)
    spent = 0.0
    per_group: dict[tuple[str, str], list[float]] = defaultdict(list)
    unknown = False
    for model in config.models:
        client = client_factory()
        for condition in config.conditions:
            for prompt in sample:
                path = transcript_root / tag / _slug(model) / condition / f"{prompt.id}__run1.jsonl"
                path.unlink(missing_ok=True)
                scored = execute_run(prompt, condition, client, model, 1, one, path)
                if scored.cost_usd is None:
                    unknown = True
                cost = scored.cost_usd or 0.0
                spent += cost
                per_group[(model, condition)].append(cost)
                log(
                    f"pilot {model} {condition} {prompt.id}: {scored.score.category}, "
                    f"${cost:.4f}" + (f" ERROR {scored.error[:60]}" if scored.error else "")
                )
    planned = len(prompts) * config.runs
    groups = {
        f"{model}/{condition}": {
            "mean_cost_per_run": sum(costs) / len(costs),
            "runs_planned": planned,
            "estimated_cost": sum(costs) / len(costs) * planned,
        }
        for (model, condition), costs in per_group.items()
    }
    estimate = sum(g["estimated_cost"] for g in groups.values())
    return {
        "pilot_runs": sum(len(c) for c in per_group.values()),
        "pilot_cost": spent,
        "groups": groups,
        "estimated_total": estimate,
        "cost_unknown": unknown,
    }


def format_pilot(result: dict[str, Any]) -> str:
    lines = [
        f"pilot: {result['pilot_runs']} runs cost ${result['pilot_cost']:.4f}",
        "estimated cost of the full run (mean cost per run x runs planned):",
    ]
    for name, group in sorted(result["groups"].items()):
        lines.append(
            f"  {name}: ${group['mean_cost_per_run']:.4f} per run x {group['runs_planned']} runs"
            f" = ${group['estimated_cost']:.2f}"
        )
    lines.append(f"estimated total: ${result['estimated_total']:.2f}")
    if result["cost_unknown"]:
        lines.append("WARNING: a model has no entry in the price table; its cost counts as 0")
    return "\n".join(lines)


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
    parser.add_argument(
        "--condition",
        choices=[*CONDITIONS, "both", "all"],
        default="both",
        help="both = raw + guarded (default); all = raw + guarded + guarded-pending",
    )
    parser.add_argument("--runs", type=int, default=3, help="runs per prompt and condition")
    parser.add_argument("--split", default="dev", help="prompt split (dev, holdout) or a YAML path")
    parser.add_argument(
        "--only", action="append", metavar="PROMPT_ID", help="run only these prompts"
    )
    parser.add_argument("--max-tool-calls", type=int, default=DEFAULT_MAX_TOOL_CALLS)
    parser.add_argument(
        "--policy",
        type=Path,
        default=POLICY_PATH,
        help="policy file for every condition; a replay needs the one the run was recorded with",
    )
    parser.add_argument(
        "--pilot",
        action="store_true",
        help="play one prompt per label once and print the estimated cost of the full run",
    )
    parser.add_argument(
        "--budget",
        type=float,
        metavar="USD",
        help="stop a live run once its cost reaches this amount",
    )
    parser.add_argument(
        "--replay", type=Path, metavar="TRANSCRIPT_DIR", help="re-score; needs no credentials"
    )
    parser.add_argument(
        "--label",
        metavar="TEXT",
        help="appended to the run's tag (lowercase letters, digits, dashes), so cells started in "
        "the same second, or that differ only by policy, never share results or transcripts",
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
    try:
        Policy.load(args.policy)
    except PolicyError as err:
        parser.error(str(err))

    if args.replay is not None:
        try:
            json_path, md_path, markdown, drift = replay(
                args.replay, prompts, split, args.results_dir, args.policy
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
    if args.budget is not None and args.budget <= 0:
        parser.error("--budget must be positive")
    conditions = {"both": DEFAULT_CONDITIONS, "all": CONDITIONS}.get(
        args.condition, (args.condition,)
    )
    config = LiveConfig(
        split=split,
        models=tuple(args.model),
        conditions=tuple(conditions),
        runs=args.runs,
        max_tool_calls=args.max_tool_calls,
        policy=args.policy,
        budget_usd=args.budget,
    )
    if args.label is not None and not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", args.label):
        parser.error("--label may only hold lowercase letters, digits and single dashes")
    tag = f"{split}-{datetime.now(UTC):%Y%m%dT%H%M%SZ}" + (f"-{args.label}" if args.label else "")
    if args.pilot:
        try:
            result = run_pilot(
                config,
                prompts,
                client_factory or make_client,
                args.transcripts_dir,
                f"pilot-{tag}",
            )
        except FatalModelError as err:
            print(f"stopped: {err}", file=sys.stderr)
            return 2
        print(format_pilot(result))
        return 0
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
