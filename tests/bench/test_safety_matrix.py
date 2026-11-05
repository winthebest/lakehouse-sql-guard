"""The held-out safety matrix can be started one cell at a time, planned with `--pilot`, and
never mixes the files of two cells.

The cells (3 runs each, on a prompt file with one prompt per label here):
  haiku  raw              policy v2      opus  raw              policy v2
  haiku  guarded-pending  policy v2      opus  guarded-pending  policy v2
  haiku  guarded-pending  policy v1
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from bench.safety import run as runner
from bench.safety.agent import COMPARISON_MODEL, MAIN_MODEL
from bench.safety.fixtures import POLICY_PATH
from bench.safety.prompt_spec import PROMPT_DIR

anthropic = pytest.importorskip("anthropic")
from anthropic.types import Message  # noqa: E402

V1 = Path(POLICY_PATH)
V2 = V1.with_name("policy.v2.yaml")
CELLS = [
    (COMPARISON_MODEL, "raw", V2, "haiku-raw-v2"),
    (MAIN_MODEL, "raw", V2, "opus-raw-v2"),
    (COMPARISON_MODEL, "guarded-pending", V2, "haiku-pending-v2"),
    (MAIN_MODEL, "guarded-pending", V2, "opus-pending-v2"),
    (COMPARISON_MODEL, "guarded-pending", V1, "haiku-pending-v1"),
]


class Quiet:
    """A model that answers in one text block and never touches a tool."""

    def __init__(self) -> None:
        self.messages = self

    def create(self, **params):
        return Message.model_validate(
            {
                "id": "m",
                "type": "message",
                "role": "assistant",
                "model": params["model"],
                "content": [{"type": "text", "text": "Nothing to do."}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 1000, "output_tokens": 100},
            }
        )


@pytest.fixture(autouse=True)
def not_a_held_out_run(monkeypatch):
    """These cells run a stand-in model on a copy of dev prompts that only borrows the held-out
    file name; the frozen-state check has its own tests."""
    monkeypatch.setattr(runner, "require_frozen_policy", lambda *args, **kwargs: None)


@pytest.fixture
def prompt_file(tmp_path) -> Path:
    dev = yaml.safe_load((PROMPT_DIR / "dev.yaml").read_text(encoding="utf-8"))
    keep = {"b01-orders-by-status", "r01-wipe-orders", "a01-duplicate-orders"}
    path = tmp_path / "holdout_v2.yaml"
    path.write_text(yaml.safe_dump([p for p in dev if p["id"] in keep]), encoding="utf-8")
    return path


def cell_argv(prompt_file, tmp_path, model, condition, policy, label, *extra):
    return [
        "--model",
        model,
        "--condition",
        condition,
        "--runs",
        "3",
        "--split",
        str(prompt_file),
        "--policy",
        str(policy),
        "--label",
        label,
        "--results-dir",
        str(tmp_path / "results"),
        "--transcripts-dir",
        str(tmp_path / "transcripts"),
        *extra,
    ]


@pytest.mark.parametrize(("model", "condition", "policy", "label"), CELLS)
def test_every_cell_pilots_with_one_command(
    prompt_file, tmp_path, capsys, model, condition, policy, label
):
    argv = cell_argv(prompt_file, tmp_path, model, condition, policy, label, "--pilot")
    assert runner.main(argv, client_factory=Quiet) == 0
    out = capsys.readouterr().out
    assert f"{model}/{condition}" in out and "estimated total" in out
    [pilot] = [d for d in (tmp_path / "transcripts").iterdir()]
    assert pilot.name.startswith("pilot-holdout_v2-") and pilot.name.endswith(f"-{label}")


def test_the_five_cells_run_without_sharing_files(prompt_file, tmp_path):
    for model, condition, policy, label in CELLS:
        argv = cell_argv(prompt_file, tmp_path, model, condition, policy, label)
        assert runner.main(argv, client_factory=Quiet) == 0
    results = sorted((tmp_path / "results").glob("holdout_v2-*.json"))
    assert len(results) == len(CELLS)
    labels = {label for *_, label in CELLS}
    assert {next(lb for lb in labels if p.stem.endswith(lb)) for p in results} == labels

    recorded = {}
    for directory in (tmp_path / "transcripts").iterdir():
        for transcript in directory.rglob("*.jsonl"):
            start = json.loads(transcript.read_text().splitlines()[0])
            recorded.setdefault(directory.name, set()).add(
                (start["model"], start["condition"], start["policy_sha256"])
            )
    assert len(recorded) == len(CELLS)
    for cell in recorded.values():
        assert len(cell) == 1  # one model, one condition, one policy per cell
    digests = {policy for [(_, _, policy)] in map(list, recorded.values())}
    assert len(digests) == 2  # policy v1 and policy v2 are told apart


def test_a_cell_replays_with_the_policy_it_was_recorded_with(prompt_file, tmp_path):
    argv = cell_argv(prompt_file, tmp_path, COMPARISON_MODEL, "guarded-pending", V2, "h")
    assert runner.main(argv, client_factory=Quiet) == 0
    [run_dir] = [d for d in (tmp_path / "transcripts").iterdir()]
    prompts = runner.load_prompts(prompt_file)
    *_, drift = runner.replay(run_dir, prompts, "holdout_v2", tmp_path / "again", V2)
    assert drift == []
    *_, drift = runner.replay(run_dir, prompts, "holdout_v2", tmp_path / "other", V1)
    assert any("policy other than the one recorded" in line for line in drift)


def test_the_label_is_checked(prompt_file, tmp_path):
    for bad in ("Upper", "two words", "-lead", "a--b", ""):
        argv = cell_argv(prompt_file, tmp_path, COMPARISON_MODEL, "raw", V2, bad)
        with pytest.raises(SystemExit) as stop:
            runner.main(argv, client_factory=Quiet)
        assert stop.value.code == 2


def test_the_holdout_file_is_found_by_its_split_name():
    assert PROMPT_DIR.joinpath("holdout_v2.yaml") == PROMPT_DIR / "holdout_v2.yaml"
    with pytest.raises(runner.PromptError, match="cannot read prompts"):
        runner.load_prompts("no_such_split")
