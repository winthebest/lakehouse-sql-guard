"""A held-out safety run refuses to start unless the classifier, policy and harness are what the
`policy-frozen-v<N>` tag froze before the prompts were written."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from bench.safety import freeze, run
from bench.safety.freeze import FrozenPolicyError, freeze_tag, require_frozen_policy

FILES = {
    "src/sqlguard/safety/guard.py": "guard",
    "src/sqlguard/core/parsing.py": "parsing",
    "config/policy.example.yaml": "v1",
    "config/policy.v2.yaml": "v2",
    "bench/safety/run.py": "harness",
    "bench/safety/scorer.py": "scorer",
    "bench/safety/tier1_cases.yaml": "cases",
    "bench/safety/prompts/dev.yaml": "dev",
    "bench/safety/prompts/holdout_v2.yaml": "seen",
    "bench/briefs/safety.md": "brief",
    "bench/cli.py": "entry",
    "uv.lock": "pins",
    "pyproject.toml": "project",
    "README.md": "not frozen",
}
V2 = "config/policy.v2.yaml"


def git(root: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(root), "-c", "user.email=a@b.c", "-c", "user.name=t", *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


def write(root: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def commit(root: Path, message: str) -> None:
    git(root, "add", "-A")
    git(root, "commit", "-qm", message)


@pytest.fixture
def frozen(tmp_path) -> Path:
    """A repository frozen at `policy-frozen-v3`, with no held-out file yet."""
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    write(root, FILES)
    commit(root, "frozen")
    git(root, "tag", "policy-frozen-v3")
    return root


@pytest.fixture
def repo(frozen) -> Path:
    """The same, given its held-out file in a later commit."""
    write(frozen, {"bench/safety/prompts/holdout_v3.yaml": "- prompts"})
    commit(frozen, "held-out prompts")
    return frozen


def check(root: Path, split="holdout_v3", policy=V2, prompt=None):
    file = root / (prompt or f"bench/safety/prompts/{Path(split).stem}.yaml")
    require_frozen_policy(split, root / policy, file, root=root)


def refusal(root: Path, **kwargs) -> str:
    with pytest.raises(FrozenPolicyError) as stop:
        check(root, **kwargs)
    return str(stop.value)


# -- which split needs which tag ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("split", "tag"),
    [
        ("holdout", "policy-frozen-v1"),
        ("holdout_v2", "policy-frozen-v2"),
        ("holdout_v3", "policy-frozen-v3"),
        ("holdout_v12", "policy-frozen-v12"),
        ("bench/safety/prompts/holdout_v3.yaml", "policy-frozen-v3"),
        ("dev", None),
    ],
)
def test_the_tag_follows_the_split_name(split, tag):
    assert freeze_tag(split) == tag


def test_dev_is_never_checked(repo):
    git(repo, "tag", "-d", "policy-frozen-v3")
    require_frozen_policy("dev", repo / V2, repo / "bench/safety/prompts/dev.yaml", root=repo)


def test_only_the_canonical_dev_file_is_exempt_by_path(repo, tmp_path):
    canonical = repo / "bench/safety/prompts/dev.yaml"
    assert freeze_tag(str(canonical), repo) is None
    assert freeze_tag("dev", repo) is None
    elsewhere = tmp_path / "somewhere" / "dev.yaml"
    elsewhere.parent.mkdir()
    assert freeze_tag(str(elsewhere), repo) == "policy-frozen-v3"
    with pytest.raises(FrozenPolicyError, match="outside the repository"):
        require_frozen_policy(str(elsewhere), repo / V2, elsewhere, root=repo)


@pytest.mark.parametrize("name", ["heldout3", "holdout-v3", "holdout_v3_copy", "mine"])
def test_a_held_out_set_cannot_slip_past_the_check_by_its_file_name(frozen, name):
    """Any file but dev is held to the newest freeze: here it was committed after the tag."""
    repo = frozen
    write(repo, {f"bench/safety/prompts/{name}.yaml": "- prompts"})
    commit(repo, "named set")
    assert freeze_tag(name, repo) == "policy-frozen-v3"
    check(repo, split=name)
    write(repo, {"src/sqlguard/safety/guard.py": "tuned"})
    commit(repo, "tune")
    assert "changed since policy-frozen-v3" in refusal(repo, split=name)


def test_an_unfamiliar_name_with_no_freeze_tag_at_all_is_refused(repo):
    git(repo, "tag", "-d", "policy-frozen-v3")
    with pytest.raises(FrozenPolicyError, match="no policy-frozen"):
        freeze_tag("mine", repo)


def test_the_newest_freeze_is_the_highest_number_not_the_last_listed(repo):
    for n in (2, 10):
        git(repo, "tag", f"policy-frozen-v{n}", "policy-frozen-v3")
    assert freeze_tag("mine", repo) == "policy-frozen-v10"


# -- the state that holds ----------------------------------------------------------------------


def test_the_frozen_state_with_the_held_out_file_committed_after_it_is_accepted(repo):
    check(repo)


def test_changes_outside_the_frozen_paths_do_not_matter(repo):
    write(repo, {"README.md": "docs moved on", "docs/new.md": "x"})
    commit(repo, "docs")
    write(repo, {"notes.txt": "untracked"})
    check(repo)


# -- every way it can fail ---------------------------------------------------------------------


def test_no_tag_means_nothing_was_frozen(repo):
    git(repo, "tag", "-d", "policy-frozen-v3")
    assert "tag policy-frozen-v3 does not exist" in refusal(repo)


def test_the_tag_must_be_in_the_history_of_head(repo):
    git(repo, "checkout", "-q", "--orphan", "elsewhere")
    git(repo, "rm", "-rqf", ".")
    write(repo, FILES | {"bench/safety/prompts/holdout_v3.yaml": "- p"})
    commit(repo, "unrelated")
    assert "not an ancestor of HEAD" in refusal(repo)


@pytest.mark.parametrize(
    "path",
    [
        "src/sqlguard/safety/guard.py",
        "src/sqlguard/core/parsing.py",
        "config/policy.v2.yaml",
        "bench/safety/run.py",
        "bench/safety/scorer.py",
        "bench/briefs/safety.md",
        "uv.lock",
        "bench/safety/prompts/holdout_v2.yaml",
    ],
)
def test_a_committed_change_after_the_freeze_is_refused(repo, path):
    write(repo, {path: "tuned"})
    commit(repo, "tune")
    assert f"changed since policy-frozen-v3: {path}" in refusal(repo)


def test_an_uncommitted_change_is_refused_too(repo):
    write(repo, {"src/sqlguard/safety/guard.py": "tuned"})
    assert "not committed: M src/sqlguard/safety/guard.py" in refusal(repo)


def test_a_new_file_under_a_frozen_path_is_refused(repo):
    write(repo, {"src/sqlguard/safety/rules.py": "new", "bench/safety/extra.py": "new"})
    message = refusal(repo)
    assert "?? src/sqlguard/safety/rules.py" in message and "?? bench/safety/extra.py" in message


def test_the_policy_must_be_a_committed_file_of_config(repo):
    assert "is not a tracked file of config/" in refusal(repo, policy="elsewhere.yaml")
    write(repo, {"config/mine.yaml": "untracked"})
    assert "is not a tracked file of config/" in refusal(repo, policy="config/mine.yaml")


def test_the_held_out_file_must_be_committed_as_it_is(repo):
    write(repo, {"bench/safety/prompts/holdout_v3.yaml": "- edited"})
    assert "holdout_v3.yaml has uncommitted changes" in refusal(repo)
    git(repo, "checkout", "--", "bench/safety/prompts/holdout_v3.yaml")
    git(repo, "mv", "bench/safety/prompts/holdout_v3.yaml", "bench/safety/prompts/other.yaml")
    commit(repo, "moved")
    write(repo, {"bench/safety/prompts/holdout_v3.yaml": "- new"})
    assert "holdout_v3.yaml is not committed" in refusal(repo)


def test_prompts_that_existed_at_the_freeze_were_not_written_after_it(repo):
    git(repo, "tag", "policy-frozen-v2", "policy-frozen-v3")
    message = refusal(repo, split="holdout_v2")
    assert "holdout_v2.yaml already existed at policy-frozen-v2" in message


def test_a_prompt_file_outside_the_repository_is_refused(repo, tmp_path):
    outside = tmp_path / "holdout_v3.yaml"
    outside.write_text("- p", encoding="utf-8")
    with pytest.raises(FrozenPolicyError, match="outside the repository"):
        require_frozen_policy("holdout_v3", repo / V2, outside, root=repo)


def test_a_directory_that_is_not_a_checkout_is_refused(tmp_path):
    with pytest.raises(FrozenPolicyError, match="not a git checkout"):
        require_frozen_policy("holdout_v3", tmp_path / V2, tmp_path / "x.yaml", root=tmp_path)


# -- the runner --------------------------------------------------------------------------------


@pytest.fixture
def stub_prompts(tmp_path) -> Path:
    path = tmp_path / "holdout_v3.yaml"
    path.write_text(
        "- id: t01\n  text: hello\n  label: benign\n  task_scope: {tables: [db.orders]}\n"
        "  expected: {done: [{type: rows_at_least, table: db.orders, value: 1}]}\n",
        encoding="utf-8",
    )
    return path


def test_a_live_run_on_a_held_out_split_stops_when_the_check_fails(
    stub_prompts, monkeypatch, capsys
):
    def refuse(*args, **kwargs):
        raise FrozenPolicyError("the frozen state of policy-frozen-v3 does not hold: x")

    monkeypatch.setattr(run, "require_frozen_policy", refuse)
    for extra in ([], ["--pilot"]):
        with pytest.raises(SystemExit) as stop:
            run.main(["--model", "m", "--split", str(stub_prompts), *extra])
        assert stop.value.code == 2
        assert "does not hold" in capsys.readouterr().err


def test_settlements_are_for_replays_only(stub_prompts, monkeypatch, capsys):
    monkeypatch.setattr(run, "require_frozen_policy", lambda *a, **k: None)
    with pytest.raises(SystemExit) as stop:
        run.main(["--model", "m", "--split", str(stub_prompts), "--settlements", "all"])
    assert stop.value.code == 2 and "--replay only" in capsys.readouterr().err


def test_a_replay_is_not_held_to_the_frozen_state(stub_prompts, tmp_path, monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("a replay must not run the frozen-state check")

    monkeypatch.setattr(run, "require_frozen_policy", refuse)
    empty = tmp_path / "transcripts"
    empty.mkdir()
    assert run.main(
        ["--replay", str(empty), "--split", str(stub_prompts), "--results-dir", str(tmp_path / "r")]
    ) in (0, 1)


def test_the_real_repository_refuses_the_second_held_out_set_now_that_the_guard_changed():
    """`policy-frozen-v2` froze the old classifier: running holdout_v2 live would no longer be
    the experiment that was pre-registered."""
    if freeze._git(
        freeze.ROOT, "rev-parse", "--verify", "--quiet", "refs/tags/policy-frozen-v2"
    ).returncode:
        pytest.skip("this clone has no policy-frozen-v2 tag")
    with pytest.raises(FrozenPolicyError):
        require_frozen_policy(
            "holdout_v2",
            freeze.ROOT / "config/policy.v2.yaml",
            freeze.ROOT / "bench/safety/prompts/holdout_v2.yaml",
        )
