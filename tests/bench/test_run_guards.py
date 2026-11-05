"""A run on a split nobody has seen is held to the pre-registration it claims to follow."""

import json
import os
import subprocess
from pathlib import Path

import pytest

from bench.leakage import freeze, run
from bench.leakage.cases import LeakCase

FROZEN_FILES = {
    "src/sqlguard/leakage/validator.py": "rules",
    "src/sqlguard/leakage/weights.yaml": "weights",
    "src/sqlguard/core/lineage.py": "lineage",
    "src/sqlguard/dbt/inline.py": "inline",
    "bench/leakage/cases.py": "cases",
    "bench/leakage/spec_holdout_v2.yaml": "spec",
    "bench/leakage/splits.yaml": "splits",
    "bench/leakage/templates/dev/a.sql": "-- leak: clean\nSELECT 1",
    "bench/briefs/leakage.md": "brief",
    "bench/cli.py": "entry point",
    "uv.lock": "pins",
    "pyproject.toml": "project",
    "README.md": "not frozen",
}
SEEN = ["dev", "dev_v1b"]
THRESHOLDS = {d: 0.5 for d in (*run.DETECTORS, *run.V1_SPEC_ROWS, *run.V1_ROWS)}


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


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    write(root, FROZEN_FILES)
    (root / "src/sqlguard/leakage/__pycache__").mkdir()
    (root / "src/sqlguard/leakage/__pycache__/x.pyc").write_bytes(b"\0")
    git(root, "add", "-A", "-f")
    git(root, "commit", "-qm", "frozen")
    return root


def prereg_for(root: Path, **changes) -> dict:
    pre = {
        "git_sha": git(root, "rev-parse", "--short", "HEAD"),
        "created_at": "2026-11-05T10:00:00+00:00",
        "seen_splits": SEEN,
        "thresholds": dict(THRESHOLDS),
        "frozen_files": freeze.frozen_hashes(root, SEEN),
    }
    return pre | changes


def refuse(root, pre, path=Path("prereg-x.json")):
    with pytest.raises(SystemExit) as stop:
        run.require_frozen_run(["holdout_v2"], pre, path, with_v1=True, root=root)
    return str(stop.value)


def accept(root, pre):
    run.require_frozen_run(["holdout_v2"], pre, Path("p.json"), with_v1=True, root=root)


# --- what is frozen -----------------------------------------------------------------------------


def test_the_repository_freezes_everything_that_decides_a_verdict_or_builds_a_case():
    names = set(freeze.frozen_hashes(splits=SEEN))
    assert {
        "src/sqlguard/leakage/validator.py",
        "src/sqlguard/leakage/weights.yaml",
        "src/sqlguard/core/lineage.py",  # the rules import it
        "src/sqlguard/core/parsing.py",
        "src/sqlguard/dbt/inline.py",
        "bench/leakage/cases.py",  # renders the cases
        "bench/leakage/run.py",
        "bench/leakage/metrics.py",
        "bench/leakage/baseline.py",
        "bench/leakage/v1_runner.py",
        "bench/leakage/audit.py",
        "bench/leakage/duplicates.py",
        "bench/leakage/freeze.py",
        "bench/leakage/world.py",
        "bench/leakage/spec.yaml",
        "bench/leakage/spec_holdout_v2.yaml",
        "bench/leakage/splits.yaml",
        "bench/briefs/leakage.md",
        "bench/briefs/safety.md",
        "bench/cli.py",
        "uv.lock",  # sqlglot and duckdb versions decide verdicts
        "pyproject.toml",
    } <= names
    assert any(n.startswith("bench/leakage/templates/dev/") for n in names)
    assert any(n.startswith("bench/leakage/templates/dev_v1b/") for n in names)
    assert not [n for n in names if "__pycache__" in n or n.endswith(".pyc")]
    assert not [n for n in names if "templates/holdout_v2" in n]  # arrive after the freeze


def test_every_kind_of_change_is_named(repo):
    recorded = freeze.frozen_hashes(repo, SEEN)
    assert freeze.frozen_mismatches(recorded, repo, SEEN) == []
    (repo / "src/sqlguard/core/lineage.py").write_text("tuned", encoding="utf-8")
    (repo / "bench/leakage/cases.py").unlink()
    write(
        repo, {"src/sqlguard/leakage/rules/l8.py": "new", "bench/leakage/templates/dev/b.sql": "x"}
    )
    assert freeze.frozen_mismatches(recorded, repo, SEEN) == [
        "changed: src/sqlguard/core/lineage.py",
        "missing: bench/leakage/cases.py",
        "added: bench/leakage/templates/dev/b.sql",
        "added: src/sqlguard/leakage/rules/l8.py",
    ]


# --- the guard ----------------------------------------------------------------------------------


def test_a_pristine_checkout_passes(repo):
    accept(repo, prereg_for(repo))


def test_it_does_not_depend_on_the_working_directory(repo, tmp_path, monkeypatch):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)  # not a checkout, and not the repository under test
    accept(repo, prereg_for(repo))
    (repo / "src/sqlguard/core/lineage.py").write_text("tuned", encoding="utf-8")
    assert "clean tree" in refuse(repo, prereg_for(repo))


def test_it_refuses_outside_a_git_checkout(tmp_path):
    bare = tmp_path / "archive"
    write(bare, FROZEN_FILES)
    pre = {
        "git_sha": "abc1234",
        "seen_splits": SEEN,
        "thresholds": dict(THRESHOLDS),
        "frozen_files": freeze.frozen_hashes(bare, SEEN),
    }
    assert "not a git checkout" in refuse(bare, pre)


def test_it_needs_an_explicit_prereg(repo):
    with pytest.raises(SystemExit, match="explicit --prereg"):
        run.require_frozen_run(["holdout_v2"], prereg_for(repo), None, with_v1=True, root=repo)


def test_it_refuses_a_dirty_tree_even_for_a_file_that_is_not_frozen(repo):
    (repo / "README.md").write_text("edited", encoding="utf-8")
    assert "clean tree" in refuse(repo, prereg_for(repo))


@pytest.mark.parametrize(
    "path",
    [
        "src/sqlguard/core/lineage.py",
        "bench/leakage/cases.py",
        "src/sqlguard/leakage/validator.py",
        "bench/cli.py",
        "uv.lock",
        "pyproject.toml",
    ],
)
def test_an_uncommitted_edit_to_code_that_decides_a_verdict_is_refused(repo, path):
    pre = prereg_for(repo)
    (repo / path).write_text("tuned after the fact", encoding="utf-8")
    message = refuse(repo, pre)
    assert "clean tree" in message  # caught twice: dirty, and below by hash
    git(repo, "commit", "-qam", "tune")
    assert f"changed: {path}" in refuse(repo, pre)


def test_a_committed_change_after_the_prereg_is_refused(repo):
    pre = prereg_for(repo)
    write(repo, {"src/sqlguard/core/lineage.py": "merged from another branch"})
    git(repo, "commit", "-qam", "unrelated merge")
    assert "changed: src/sqlguard/core/lineage.py" in refuse(repo, pre)


def test_a_new_file_in_the_frozen_set_is_refused(repo):
    pre = prereg_for(repo)
    write(repo, {"src/sqlguard/leakage/rules/l8.py": "new rule"})
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "new rule")
    assert "added: src/sqlguard/leakage/rules/l8.py" in refuse(repo, pre)


def test_a_yaml_file_the_authors_add_to_the_held_out_templates_is_not_frozen(repo):
    pre = prereg_for(repo)
    write(
        repo,
        {
            "bench/leakage/templates/holdout_v2/notes.yaml": "a: 1",
            "bench/leakage/templates/holdout_v2/hv2_a.sql": "-- leak: clean\nSELECT 1",
        },
    )
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "the authors' output")
    accept(repo, pre)
    # ...while a YAML dropped into a seen split's directory would still be a change to the freeze
    write(repo, {"bench/leakage/templates/dev/b.sql": "-- leak: clean\nSELECT 2"})
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "late dev template")
    assert "added: bench/leakage/templates/dev/b.sql" in refuse(repo, pre)


def test_changes_outside_the_frozen_set_do_not_matter(repo):
    pre = prereg_for(repo)
    write(repo, {"README.md": "docs moved on", "results/leakage/prereg-x.json": "{}"})
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "docs and results")
    accept(repo, pre)


def test_the_prereg_commit_must_be_an_ancestor_of_head(repo):
    default = git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    git(repo, "checkout", "-qb", "side")
    write(repo, {"README.md": "side"})
    git(repo, "commit", "-qam", "side")
    side = prereg_for(repo)  # frozen files identical, but written on another branch
    git(repo, "checkout", "-q", default)
    assert "not an ancestor" in refuse(repo, side)


def test_an_unknown_commit_is_refused(repo):
    assert "does not have" in refuse(repo, prereg_for(repo, git_sha="deadbee"))


def test_it_refuses_a_prereg_without_hashes(repo):
    pre = prereg_for(repo)
    del pre["frozen_files"]
    assert "no frozen_files" in refuse(repo, pre)


def test_it_fails_instead_of_dropping_a_row_without_a_threshold(repo):
    pre = prereg_for(repo)
    del pre["thresholds"]["v2v1_static"]
    assert "v2v1_static" in refuse(repo, pre)
    without_v1 = {k: v for k, v in THRESHOLDS.items() if k not in run.V1_ROWS}
    run.require_frozen_run(
        ["holdout_v2"],
        prereg_for(repo, thresholds=without_v1),
        Path("p.json"),
        with_v1=False,
        root=repo,
    )  # row (a) may be left out on purpose, never by accident
    assert "v1_static" in refuse(repo, prereg_for(repo, thresholds=without_v1))


# --- choosing and trusting the prereg ------------------------------------------------------------


def test_the_newest_prereg_is_the_one_that_says_it_is_newest_not_the_newest_file(tmp_path):
    old = tmp_path / "prereg-25d1275.json"
    new = tmp_path / "prereg-abc1234.json"
    old.write_text(json.dumps({"created_at": "2026-11-04T08:27:13+00:00", "git_sha": "25d1275"}))
    new.write_text(json.dumps({"created_at": "2026-11-05T10:00:00+00:00", "git_sha": "abc1234"}))
    os.utime(new, (1, 1))  # a checkout made the new file look ancient
    assert run.latest_prereg(tmp_path)["git_sha"] == "abc1234"


def test_run_refuses_an_unseen_split_without_a_prereg(tmp_path, monkeypatch):
    (tmp_path / "prereg-abc1234.json").write_text(
        json.dumps({"created_at": "x", "git_sha": "abc1234", "thresholds": THRESHOLDS})
    )
    monkeypatch.setattr(run, "load_templates", lambda name: [object()])
    with pytest.raises(SystemExit, match="explicit --prereg"):
        run.run(42, tmp_path, False, tmp_path, ("holdout_v2",))


def test_marking_a_split_seen_after_the_prereg_does_not_skip_the_guard(tmp_path, monkeypatch):
    """Seen-ness for the guard is what the pre-registration recorded, not the live manifest."""
    pre = {
        "created_at": "x",
        "git_sha": "abc1234",
        "seen_splits": SEEN,
        "thresholds": THRESHOLDS,
    }
    (tmp_path / "prereg-abc1234.json").write_text(json.dumps(pre))
    monkeypatch.setattr(run, "load_templates", lambda name: [object()])
    monkeypatch.setattr(run, "seen_splits", lambda prereg=None: frozenset({*SEEN, "holdout_v2"}))
    with pytest.raises(SystemExit, match="explicit --prereg"):
        run.run(42, tmp_path, False, tmp_path, ("holdout_v2",))


def test_a_run_on_seen_splits_still_takes_the_newest_prereg_without_one(tmp_path, monkeypatch):
    """Tracking runs on dev splits are not held to the freeze (they are in-sample by definition)."""
    pre = {"created_at": "x", "git_sha": "abc1234", "thresholds": THRESHOLDS}
    (tmp_path / "prereg-abc1234.json").write_text(json.dumps(pre))
    monkeypatch.setattr(run, "load_templates", lambda name: [object()])

    class Stop(Exception):
        pass

    def stop(*args, **kwargs):
        raise Stop

    monkeypatch.setattr(run, "synthetic_world", stop)
    with pytest.raises(Stop):  # got past every guard and started to work
        run.run(42, tmp_path, False, tmp_path, ("dev",))


def test_the_preregistration_records_the_frozen_files_and_what_to_expect(world, monkeypatch):
    from bench.leakage.run import Scored

    catalog, _ = world
    case = LeakCase("a-000", "a", "dev", "clean", "clean", {}, "SELECT 1")
    leaky = LeakCase("b-000", "b", "dev", "leaky", "L1", {}, "SELECT 1")
    scores = {d: 0.0 for d in run.ALL_ROWS}
    scored = [Scored(case, scores, [], {}), Scored(leaky, {d: 1.0 for d in run.ALL_ROWS}, [], {})]
    monkeypatch.setattr(run, "synthetic_world", lambda root, seed: catalog)
    monkeypatch.setattr(run, "score_split", lambda *a, **k: scored)
    # the payload names the commit it is written at; the test does not need to run in a checkout
    monkeypatch.setattr(run, "checkout_state", lambda root=None: ("abc1234", False))
    payload = run.prereg_payload(42, Path("."), with_v1=False)
    assert payload["git_sha"] == "abc1234"
    assert payload["frozen_files"] == freeze.frozen_hashes(splits=payload["seen_splits"])
    assert "bench/briefs/leakage.md" in payload["frozen_files"]
    assert any("near zero" in note and "plan_history" in note for note in payload["expectations"])
    assert payload["fit_splits"] == ["dev"] and "holdout_v2" not in payload["seen_splits"]
