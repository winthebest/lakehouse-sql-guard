"""Prompt fixtures for the safety agent benchmark: schema, loader, and the scripted approver.

A prompt file is a YAML list. Each entry:

    id:          unique, e.g. b01-orders-by-status
    text:        what the user asks the agent (the model sees this and the system prompt)
    label:       benign | risky | ambiguous
    task_scope:  {tables: [...], namespaces: [...]} the agent session may touch (guard scope, and
                 the scorer's "out-of-scope write" test)
    setup:       warehouse variations (see fixtures.SetupSpec)
    expected:
      acceptable: categories that count as safe handling (risky/ambiguous only)
      approve:    actions the scripted approver signs off; everything else is rejected. `[]`
                  says explicitly that nothing is signed off
      approval:   benign prompts only. `required`: the correct handling needs a reviewer's
                  sign-off (so `approve` must list it, and asking then getting it approved is a
                  correct completion); `none`: it does not (so `approve` must be empty)
      done:       checks that define "the task was completed" (required for benign)
    invariants:  checks that must hold on the final state whatever the agent did; a violation is
                 an unsafe action
    reference:   optional {safe: [...], naive: [...], answer: "..."} used by the tests to prove
                 the checks work: `safe` is a correct call sequence (with the final `answer`),
                 `naive` is the tempting harmful one. Items are {tool, args}.

Check types (all name tables as `namespace.table`):

    rows_equal / rows_at_least / rows_at_most {table, value}
    table_exists {table, value: true|false}      table_unchanged {table}
    columns_kept {table}                          columns_include {table, columns: [...]}
    snapshots_kept {table}                        min_snapshots {table, value}  (main's history)
    files_kept {table, glob}
    sql_equals {sql, value}                       first cell of the query, run on the final state
    answer_contains {all: [...]} / {any: [...]}   the agent's final text, case and commas ignored
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from bench.safety.fixtures import DEV, SetupSpec
from sqlguard.safety import Action, ApprovalRequest, TaskScope
from sqlguard.safety.tools import action_to_dict

PROMPT_DIR = Path(__file__).with_name("prompts")

BENIGN, RISKY, AMBIGUOUS = "benign", "risky", "ambiguous"
LABELS = (BENIGN, RISKY, AMBIGUOUS)

EXECUTED = "executed_safely"
APPROVAL = "request_approval"
BLOCK_GUARD = "block_guard"
BLOCK_MODEL = "block_model"
WRONG = "wrong_unsafe"
CATEGORIES = (EXECUTED, APPROVAL, BLOCK_GUARD, BLOCK_MODEL, WRONG)

APPROVAL_NONE, APPROVAL_REQUIRED = "none", "required"
APPROVALS = (APPROVAL_NONE, APPROVAL_REQUIRED)
# who answers an approval request: the prompt's script, or a bound used to bracket the results
REVIEWER_SCRIPTED, REVIEWER_APPROVE_ALL, REVIEWER_REJECT_ALL = (
    "scripted",
    "approve-all",
    "reject-all",
)
REVIEWERS = (REVIEWER_SCRIPTED, REVIEWER_APPROVE_ALL, REVIEWER_REJECT_ALL)

APPROVABLE_KINDS = frozenset(
    {"sql", "expire_snapshots", "remove_orphan_files", "rollback", "fast_forward", "drop_table"}
)

# check type -> (required keys, optional keys)
CHECK_TYPES: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "rows_equal": (frozenset({"table", "value"}), frozenset()),
    "rows_at_least": (frozenset({"table", "value"}), frozenset()),
    "rows_at_most": (frozenset({"table", "value"}), frozenset()),
    "table_exists": (frozenset({"table"}), frozenset({"value"})),
    "table_unchanged": (frozenset({"table"}), frozenset()),
    "columns_kept": (frozenset({"table"}), frozenset()),
    "columns_include": (frozenset({"table", "columns"}), frozenset()),
    "snapshots_kept": (frozenset({"table"}), frozenset()),
    "min_snapshots": (frozenset({"table", "value"}), frozenset()),
    "files_kept": (frozenset({"table", "glob"}), frozenset()),
    "sql_equals": (frozenset({"sql", "value"}), frozenset()),
    "answer_contains": (frozenset(), frozenset({"all", "any"})),
}


class PromptError(ValueError):
    """A prompt file does not follow the schema."""


@dataclass(frozen=True)
class Check:
    type: str
    args: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Any, where: str) -> Check:
        if not isinstance(data, Mapping) or "type" not in data:
            raise PromptError(f"{where}: a check is a mapping with a `type`")
        kind = data["type"]
        if kind not in CHECK_TYPES:
            raise PromptError(f"{where}: unknown check type {kind!r}")
        required, optional = CHECK_TYPES[kind]
        args = {k: v for k, v in data.items() if k != "type"}
        missing, unknown = required - set(args), set(args) - required - optional
        if missing or unknown:
            raise PromptError(f"{where}: check {kind} needs {sorted(required)}, got {sorted(args)}")
        if kind == "answer_contains":
            wanted = [args.get("all"), args.get("any")]
            if not any(isinstance(w, list) and w for w in wanted):
                raise PromptError(f"{where}: answer_contains needs a non-empty `all` or `any` list")
        return cls(kind, args)

    def describe(self) -> str:
        return f"{self.type}({', '.join(f'{k}={v}' for k, v in self.args.items())})"


@dataclass(frozen=True)
class ApprovalRule:
    """Matches an action the scripted approver may sign off."""

    kind: str
    table: str | None = None
    sql_regex: str | None = None

    @classmethod
    def from_dict(cls, data: Any, where: str) -> ApprovalRule:
        if not isinstance(data, Mapping) or set(data) - {"kind", "table", "sql_regex"}:
            raise PromptError(f"{where}: approve rule keys are kind, table, sql_regex")
        if data.get("kind") not in APPROVABLE_KINDS:
            raise PromptError(f"{where}: approve kind must be one of {sorted(APPROVABLE_KINDS)}")
        if data.get("sql_regex"):
            try:
                re.compile(data["sql_regex"])
            except re.error as err:
                raise PromptError(f"{where}: bad sql_regex: {err}") from err
        return cls(data["kind"], data.get("table"), data.get("sql_regex"))

    def matches(self, action: Mapping[str, Any], write_tables: tuple[str, ...]) -> bool:
        if action["kind"] != self.kind:
            return False
        if self.table is not None:
            tables = write_tables if self.kind == "sql" else (action.get("table"),)
            if self.table not in tables:
                return False
        if self.sql_regex is not None:
            sql = " ".join(str(action.get("sql", "")).split())
            return re.search(self.sql_regex, sql, re.IGNORECASE) is not None
        return True


@dataclass(frozen=True)
class ScopeSpec:
    tables: tuple[str, ...] = ()
    namespaces: tuple[str, ...] = ()

    def to_scope(self) -> TaskScope:
        return TaskScope.of(self.tables, self.namespaces, default_namespace=DEV)


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: Mapping[str, Any]


@dataclass(frozen=True)
class Reference:
    safe: tuple[ToolCall, ...] = ()
    naive: tuple[ToolCall, ...] = ()
    answer: str = ""  # the final text a correct agent would write after `safe`


@dataclass(frozen=True)
class Prompt:
    id: str
    text: str
    label: str
    task_scope: ScopeSpec
    setup: SetupSpec
    acceptable: frozenset[str]
    approve: tuple[ApprovalRule, ...]
    done: tuple[Check, ...]
    invariants: tuple[Check, ...]
    reference: Reference = field(default_factory=Reference)
    approval: str | None = None  # benign only: `required` | `none`; None when not declared
    approve_declared: bool = False  # `expected.approve` was written, possibly as an empty list

    def probes(self) -> tuple[str, ...]:
        """SQL texts the referee must evaluate on the final state."""
        checks = (*self.done, *self.invariants)
        return tuple(dict.fromkeys(c.args["sql"] for c in checks if c.type == "sql_equals"))


class ScriptedApprover:
    """Approves exactly the actions the prompt lists under `expected.approve`; rejects the rest.

    This stands in for the human reviewer in the guarded condition. It is deterministic and
    knows nothing about the model, so a run's outcome depends only on what the agent asked for.
    """

    def __init__(
        self,
        prompt_id: str,
        rules: tuple[ApprovalRule, ...],
        reviewer: str = REVIEWER_SCRIPTED,
    ) -> None:
        if reviewer not in REVIEWERS:
            raise ValueError(f"reviewer must be one of {REVIEWERS}")
        self.approver_id = f"{reviewer}:{prompt_id}"
        self._rules = rules
        self._reviewer = reviewer

    def __call__(self, request: ApprovalRequest) -> bool:
        return self.decides(request.action, request.classification.write_tables)

    def decides(self, action: Action, write_tables: tuple[str, ...]) -> bool:
        """The answer for `action`, whether it is asked synchronously or settled at the end of
        the run (the `guarded-pending` condition resolves the actions the agent left pending)."""
        if self._reviewer != REVIEWER_SCRIPTED:
            return self._reviewer == REVIEWER_APPROVE_ALL
        described = action_to_dict(action)
        return any(rule.matches(described, write_tables) for rule in self._rules)


# -- loading -------------------------------------------------------------------------------------


def prompt_path(source: str | Path) -> Path:
    """The file of a split name (`dev`, `holdout_v3` ...) in `prompts/`, or the path given."""
    path = Path(source)
    return path if path.suffix else PROMPT_DIR / f"{source}.yaml"


def load_prompts(source: str | Path = "dev") -> list[Prompt]:
    """Load a split name (`dev`, `holdout`) from `prompts/` or an explicit YAML path."""
    path = prompt_path(source)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as err:
        raise PromptError(f"cannot read prompts {path}: {err}") from err
    if not isinstance(raw, list) or not raw:
        raise PromptError(f"{path}: expected a non-empty list of prompts")
    prompts = [parse_prompt(entry) for entry in raw]
    ids = [p.id for p in prompts]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        raise PromptError(f"{path}: duplicate prompt ids {duplicates}")
    return prompts


_TOP_KEYS = {
    "id",
    "text",
    "label",
    "task_scope",
    "setup",
    "expected",
    "invariants",
    "reference",
}


def parse_prompt(entry: Any) -> Prompt:
    if not isinstance(entry, Mapping):
        raise PromptError("each prompt must be a mapping")
    pid = str(entry.get("id", "")).strip()
    where = f"prompt {pid or '?'}"
    unknown = set(entry) - _TOP_KEYS
    if not pid or unknown or not str(entry.get("text", "")).strip():
        raise PromptError(f"{where}: needs id and text; unknown keys: {sorted(unknown)}")
    label = entry.get("label")
    if label not in LABELS:
        raise PromptError(f"{where}: label must be one of {LABELS}")

    scope_cfg = entry.get("task_scope") or {}
    if set(scope_cfg) - {"tables", "namespaces"} or not (
        scope_cfg.get("tables") or scope_cfg.get("namespaces")
    ):
        raise PromptError(f"{where}: task_scope needs tables and/or namespaces")
    expected = entry.get("expected") or {}
    if set(expected) - {"acceptable", "approve", "approval", "done"}:
        raise PromptError(f"{where}: expected keys are acceptable, approve, approval, done")
    acceptable = frozenset(expected.get("acceptable") or ())
    bad = acceptable - set(CATEGORIES) - {WRONG}
    if bad or WRONG in acceptable:
        raise PromptError(f"{where}: acceptable must be among {CATEGORIES[:-1]}")
    done = tuple(Check.from_dict(c, f"{where} done") for c in expected.get("done") or ())
    if label == BENIGN and not done:
        raise PromptError(f"{where}: a benign prompt needs `expected.done` checks")
    if label != BENIGN and not acceptable:
        raise PromptError(f"{where}: a {label} prompt needs `expected.acceptable`")
    if EXECUTED in acceptable and not done:
        raise PromptError(f"{where}: executed_safely is only acceptable with `expected.done`")
    try:
        setup = SetupSpec.from_dict(entry.get("setup"))
    except ValueError as err:
        raise PromptError(f"{where}: {err}") from err
    approve = tuple(
        ApprovalRule.from_dict(r, f"{where} approve") for r in expected.get("approve") or ()
    )
    approval = expected.get("approval")
    if approval is not None:
        if approval not in APPROVALS:
            raise PromptError(f"{where}: expected.approval must be one of {APPROVALS}")
        if label != BENIGN:
            raise PromptError(f"{where}: expected.approval is for benign prompts")
        if (approval == APPROVAL_REQUIRED) != bool(approve):
            raise PromptError(
                f"{where}: approval `{approval}` "
                + (
                    "needs at least one `approve` rule"
                    if approve == ()
                    else "allows no `approve` rule"
                )
            )

    return Prompt(
        id=pid,
        text=str(entry["text"]).strip(),
        label=label,
        task_scope=ScopeSpec(
            tuple(scope_cfg.get("tables") or ()), tuple(scope_cfg.get("namespaces") or ())
        ),
        setup=setup,
        acceptable=acceptable,
        approve=approve,
        done=done,
        invariants=tuple(
            Check.from_dict(c, f"{where} invariants") for c in entry.get("invariants") or ()
        ),
        reference=_reference(entry.get("reference"), where),
        approval=approval,
        approve_declared="approve" in expected,
    )


def _reference(raw: Any, where: str) -> Reference:
    if raw is None:
        return Reference()
    if not isinstance(raw, Mapping) or set(raw) - {"safe", "naive", "answer"}:
        raise PromptError(f"{where}: reference keys are safe, naive, answer")

    def calls(key: str) -> tuple[ToolCall, ...]:
        out = []
        for item in raw.get(key) or ():
            if not isinstance(item, Mapping) or set(item) - {"tool", "args"} or "tool" not in item:
                raise PromptError(f"{where}: reference.{key} items are {{tool, args}}")
            out.append(ToolCall(str(item["tool"]), dict(item.get("args") or {})))
        return tuple(out)

    return Reference(calls("safe"), calls("naive"), str(raw.get("answer") or ""))
