"""Validity gate for a prompt file: does the safe path of each benign prompt need a signature
nobody scripted?

The guarded conditions answer every approval request with the prompt's `expected.approve` rules
and reject the rest. A benign prompt whose correct handling includes an action the policy sends
to a reviewer (a rollback, expiring snapshots, a write to production, a schema change) but that
declares no matching rule is therefore unfinishable under the guard: the scripted reviewer
rejects the step the task needs, and the run counts as a false block.

    uv run python -m bench.safety.approval_gate --prompts bench/safety/prompts/holdout_v3.yaml \\
        --policy config/policy.v2.yaml

For every benign and ambiguous prompt it plays `reference.safe` through the guard under the given
policy with a reviewer that signs off everything and records what it was asked. Each approval
request must match an `expected.approve` rule of the prompt. A benign prompt whose reference step
the guard refuses outright (a denial, an unsupported action) is reported too: its safe path cannot
be completed either. Risky prompts are not checked: for them rejection is the point.

With `--strict` (the mode for a held-out candidate file, and the one the author brief
describes) every benign prompt must also say what it expects of the reviewer:
`expected.approval: required` when its safe path goes to the reviewer
(with the matching `approve` rules), `expected.approval: none` when it does not, and an explicit
`approve: []` when nothing is signed off. A claim the safe path does not bear out is reported.

Exit status 0 when nothing is reported, 1 when a prompt has a gap, 2 for a bad invocation.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from bench.safety.agent import dispatch
from bench.safety.fixtures import POLICY_PATH, temp_world
from bench.safety.prompt_spec import (
    AMBIGUOUS,
    APPROVAL_NONE,
    APPROVAL_REQUIRED,
    BENIGN,
    Prompt,
    PromptError,
    load_prompts,
)
from sqlguard.safety import ApprovalRequest
from sqlguard.safety.tools import GuardedBackend, ToolResult

CHECKED_LABELS = (BENIGN, AMBIGUOUS)
# what the guard answers when it will not even ask a reviewer
_REFUSED_STATUSES = frozenset({"blocked", "error", "invalid_arguments", "approval_rejected"})


@dataclass(frozen=True)
class Gap:
    """One reference step that the prompt's `expected.approve` does not cover."""

    prompt_id: str
    label: str
    step: int  # position in reference.safe, from 1
    tool: str
    kind: str  # "unapproved" | "refused" | "undeclared" | "mismatch" (strict only)
    detail: str  # what was asked for, or why the step was refused

    def line(self) -> str:
        what = {
            "unapproved": "needs a reviewer's approval and has no expected.approve rule",
            "refused": "was refused by the guard",
            "undeclared": "does not declare what it expects of the reviewer",
            "mismatch": "declares an expectation of the reviewer its safe path does not bear out",
        }[self.kind]
        if self.step == 0:
            return f"{self.prompt_id} ({self.label}) {what}: {self.detail}"
        return f"{self.prompt_id} ({self.label}) step {self.step} {self.tool} {what}: {self.detail}"


class _SignsOffEverything:
    approver_id = "gate"

    def __call__(self, request: ApprovalRequest) -> bool:
        return True


def check_prompt(
    prompt: Prompt, policy: Path | str = POLICY_PATH, *, strict: bool = False
) -> list[Gap]:
    """The gaps of one prompt under `policy` (empty for a risky prompt or a clean one)."""
    if prompt.label not in CHECKED_LABELS:
        return []
    gaps: list[Gap] = []
    asked = 0
    with temp_world(prompt.setup, policy) as world:
        backend = GuardedBackend(world.guard(), prompt.task_scope.to_scope(), _SignsOffEverything())
        for step, call in enumerate(prompt.reference.safe, start=1):
            result = dispatch(backend, call.name, dict(call.arguments))
            asked += len(result.approvals)
            gaps += _gaps_of(prompt, step, call.name, result)
    if strict and prompt.label == BENIGN:
        gaps += _declaration_gaps(prompt, asked)
    return gaps


def _declaration_gaps(prompt: Prompt, asked: int) -> list[Gap]:
    def gap(kind: str, detail: str) -> Gap:
        return Gap(prompt.id, prompt.label, 0, "", kind, detail)

    found = []
    if not prompt.approve_declared:
        found.append(gap("undeclared", "write `expected.approve: []` when nothing is signed off"))
    if prompt.approval is None:
        found.append(gap("undeclared", "write `expected.approval: required` or `none`"))
    elif asked and prompt.approval != APPROVAL_REQUIRED:
        found.append(
            gap(
                "mismatch",
                f"its safe path asks the reviewer {asked} time(s), so it "
                f"needs `expected.approval: required`, not `{prompt.approval}`",
            )
        )
    elif not asked and prompt.approval == APPROVAL_REQUIRED:
        found.append(
            gap(
                "mismatch",
                "`expected.approval: required`, but its safe path never "
                f"asks the reviewer; use `{APPROVAL_NONE}`",
            )
        )
    return found


def _gaps_of(prompt: Prompt, step: int, tool: str, result: ToolResult) -> list[Gap]:
    found = [
        Gap(prompt.id, prompt.label, step, tool, "unapproved", _describe(event.action, event))
        for event in result.approvals
        if not any(rule.matches(event.action, event.tables) for rule in prompt.approve)
    ]
    if prompt.label == BENIGN and result.status in _REFUSED_STATUSES:
        outcome = (result.guard or {}).get("outcome", result.status)
        reasons = "; ".join(result.payload.get("reasons") or [result.payload.get("error", "")])
        found.append(Gap(prompt.id, prompt.label, step, tool, "refused", f"{outcome}: {reasons}"))
    return found


def _describe(action: dict, event) -> str:
    text = " ".join(str(action.get("sql") or action.get("table") or "").split())
    reason = "; ".join(event.reasons) or event.klass
    return f"{action['kind']} {text[:90]!r} ({reason[:200]})"


def check_prompts(
    prompts: Iterable[Prompt], policy: Path | str = POLICY_PATH, *, strict: bool = False
) -> list[Gap]:
    return [gap for prompt in prompts for gap in check_prompt(prompt, policy, strict=strict)]


def format_report(prompts: Sequence[Prompt], gaps: Sequence[Gap]) -> str:
    checked = [p for p in prompts if p.label in CHECKED_LABELS]
    if not gaps:
        return (
            f"ok: {len(checked)} benign and ambiguous prompts need no approval they do not declare"
        )
    flagged = sorted({g.prompt_id for g in gaps})
    lines = [
        f"{len(flagged)} of {len(checked)} benign and ambiguous prompts have a gap "
        f"({len(gaps)} steps):"
    ]
    lines += [f"  {gap.line()}" for gap in gaps]
    lines.append(
        "add an expected.approve rule for each unapproved step (the reviewer signs it off) and "
        "declare expected.approval, or change the reference so the task does not need one"
    )
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m bench.safety.approval_gate",
        description="Report benign prompts whose safe path needs an approval they do not declare.",
    )
    parser.add_argument("--prompts", required=True, help="prompt split name or YAML file")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="also require every benign prompt to declare expected.approval and expected.approve",
    )
    parser.add_argument(
        "--policy",
        type=Path,
        default=POLICY_PATH,
        help="the policy file the benchmark runs with (default: the benchmark's default)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        prompts = load_prompts(args.prompts)
    except PromptError as err:
        parser.error(str(err))
    if not args.policy.is_file():
        parser.error(f"no policy file {args.policy}")
    gaps = check_prompts(prompts, args.policy, strict=args.strict)
    print(format_report(prompts, gaps))
    return 1 if gaps else 0


if __name__ == "__main__":
    sys.exit(main())
