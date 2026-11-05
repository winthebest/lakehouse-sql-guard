"""A run on a split nobody has seen is held to the pre-registration it claims to follow."""

import json
import os
from pathlib import Path

import pytest

from bench.leakage import freeze, run
from bench.leakage.cases import LeakCase

FROZEN_FILES = {
    "src/sqlguard/leakage/validator.py": "rules",
    "src/sqlguard/leakage/weights.yaml": "weights",
    "src/sqlguard/dbt/inline.py": "inline",
    "bench/leakage/spec_holdout_v2.yaml": "spec",
    "bench/leakage/splits.yaml": "splits",
    "bench/leakage/world.py": "world",
}
THRESHOLDS = {d: 0.5 for d in (*run.DETECTORS, *run.V1_SPEC_ROWS, *run.V1_ROWS)}


@pytest.fixture
def tree(tmp_path):
    for name, text in FROZEN_FILES.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    (tmp_path / "src/sqlguard/leakage/__pycache__").mkdir()
    (tmp_path / "src/sqlguard/leakage/__pycache__/x.pyc").write_bytes(b"\0")
    return tmp_path


@pytest.fixture
def clean(monkeypatch):
    monkeypatch.setattr(run, "git_sha", lambda: ("abc1234", False))


def prereg_for(tree: Path, **changes) -> dict:
    pre = {
        "git_sha": "abc1234",
        "created_at": "2026-11-05T10:00:00+00:00",
        "thresholds": dict(THRESHOLDS),
        "frozen_files": freeze.frozen_hashes(tree),
    }
    return pre | changes


def refuse(tree, pre, path=Path("prereg-abc1234.json"), **kwargs):
    with pytest.raises(SystemExit) as stop:
        run.require_frozen_run(["holdout_v2"], pre, path, with_v1=True, root=tree, **kwargs)
    return str(stop.value)


def test_the_repository_freezes_the_rules_the_specs_and_the_world():
    names = set(freeze.frozen_hashes())
    assert {
        "src/sqlguard/leakage/validator.py",
        "src/sqlguard/leakage/weights.yaml",
        "src/sqlguard/dbt/inline.py",
        "bench/leakage/spec_holdout_v2.yaml",
        "bench/leakage/splits.yaml",
        "bench/leakage/world.py",
    } <= names
    assert not [n for n in names if "__pycache__" in n or n.endswith(".pyc")]


def test_an_unchanged_tree_matches_and_every_kind_of_change_is_named(tree):
    recorded = freeze.frozen_hashes(tree)
    assert freeze.frozen_mismatches(recorded, tree) == []
    (tree / "src/sqlguard/leakage/weights.yaml").write_text("heavier", encoding="utf-8")
    (tree / "bench/leakage/world.py").unlink()
    (tree / "src/sqlguard/leakage/rules").mkdir()
    (tree / "src/sqlguard/leakage/rules/l8.py").write_text("new", encoding="utf-8")
    assert freeze.frozen_mismatches(recorded, tree) == [
        "changed: src/sqlguard/leakage/weights.yaml",
        "missing: bench/leakage/world.py",
        "added: src/sqlguard/leakage/rules/l8.py",
    ]


def test_a_run_on_an_unseen_split_passes_when_nothing_changed(tree, clean):
    run.require_frozen_run(
        ["holdout_v2"], prereg_for(tree), Path("p.json"), with_v1=True, root=tree
    )


def test_it_needs_an_explicit_prereg(tree, clean):
    with pytest.raises(SystemExit, match="explicit --prereg"):
        run.require_frozen_run(["holdout_v2"], prereg_for(tree), None, with_v1=True, root=tree)


def test_it_refuses_a_dirty_tree(tree, monkeypatch):
    monkeypatch.setattr(run, "git_sha", lambda: ("abc1234", True))
    assert "clean tree" in refuse(tree, prereg_for(tree))


def test_it_refuses_when_a_frozen_file_changed_even_uncommitted(tree, clean):
    pre = prereg_for(tree)
    (tree / "src/sqlguard/leakage/validator.py").write_text(
        "tuned after the fact", encoding="utf-8"
    )
    message = refuse(tree, pre)
    assert "changed: src/sqlguard/leakage/validator.py" in message


def test_it_refuses_a_prereg_without_hashes(tree, clean):
    pre = prereg_for(tree)
    del pre["frozen_files"]
    assert "no frozen_files" in refuse(tree, pre)


def test_it_fails_instead_of_dropping_a_row_without_a_threshold(tree, clean):
    pre = prereg_for(tree)
    del pre["thresholds"]["v2v1_static"]
    assert "refusing to drop rows" in refuse(tree, pre) and "v2v1_static" in refuse(tree, pre)
    without_v1 = {k: v for k, v in THRESHOLDS.items() if k not in run.V1_ROWS}
    run.require_frozen_run(
        ["holdout_v2"],
        prereg_for(tree, thresholds=without_v1),
        Path("p.json"),
        with_v1=False,
        root=tree,
    )  # row (a) may be left out on purpose, never by accident
    assert "v1_static" in refuse(tree, prereg_for(tree, thresholds=without_v1))


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
    payload = run.prereg_payload(42, Path("."), with_v1=False)
    assert payload["frozen_files"] == freeze.frozen_hashes()
    assert payload["recorded_files"] == freeze.recorded_hashes()
    assert any("near zero" in note and "plan_history" in note for note in payload["expectations"])
    assert payload["fit_splits"] == ["dev"] and "holdout_v2" not in payload["seen_splits"]


def test_the_preregistration_also_records_the_briefs_and_the_gating_tools():
    recorded = freeze.recorded_hashes()
    assert {
        "bench/briefs/leakage.md",
        "bench/briefs/safety.md",
        "bench/leakage/splits.yaml",
        "bench/leakage/audit.py",
        "bench/leakage/duplicates.py",
        "bench/leakage/freeze.py",
    } <= set(recorded)
    assert all(len(h) == 64 for h in recorded.values())


def test_a_changed_brief_is_reported_not_refused(tmp_path, clean):
    brief = tmp_path / "bench/briefs/leakage.md"
    brief.parent.mkdir(parents=True)
    brief.write_text("v1", encoding="utf-8")
    recorded = freeze.recorded_hashes(tmp_path)
    assert freeze.recorded_differences(recorded, tmp_path) == []
    brief.write_text("v2", encoding="utf-8")
    (tmp_path / "bench/leakage").mkdir(parents=True)
    (tmp_path / "bench/leakage/audit.py").write_text("new tool", encoding="utf-8")
    assert freeze.recorded_differences(recorded, tmp_path) == [
        "changed: bench/briefs/leakage.md",
        "added: bench/leakage/audit.py",
    ]
    # the frozen checks are untouched by it
    rule = tmp_path / "src/sqlguard/leakage/rule.py"
    rule.parent.mkdir(parents=True)
    rule.write_text("rule", encoding="utf-8")
    pre = prereg_for(tmp_path)
    run.require_frozen_run(["holdout_v2"], pre, Path("p.json"), with_v1=True, root=tmp_path)
