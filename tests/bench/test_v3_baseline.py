"""The verdicts the v3 rules gave every committed spec and split are recorded and complete."""

import json
from pathlib import Path

import pytest

from bench.leakage import snapshot
from bench.leakage.cases import generate_cases, load_templates, render
from bench.leakage.splits import load_splits

BASELINE = Path(__file__).parent / "data" / "v3-baseline-verdicts.json"
ROUNDS = {"round1": 8, "round2": 18}


@pytest.fixture(scope="module")
def baseline():
    return json.loads(BASELINE.read_text(encoding="utf-8"))


def test_the_baseline_names_the_frozen_rules_it_was_taken_with(baseline):
    assert baseline["rules_tag"] == "rules-frozen-v3" and baseline["seed"] == 42
    assert set(baseline) == {"rules_tag", "seed", "leakage", "realworld"}


def test_the_baseline_holds_every_case_of_every_split(baseline):
    recorded = baseline["leakage"]
    assert set(recorded) == {*load_splits(), "real"}
    for split in load_splits():
        expected = {c.case_id for c in generate_cases(42, (split,))}
        assert set(recorded[split]) == expected, split
    real = {c.case_id for t in load_templates("real") for c in render(t)}
    assert set(recorded["real"]) == real
    for split in recorded.values():
        for reading in split.values():
            assert set(reading) == {"static", "combined"}
            for verdict, score, rules in reading.values():
                assert isinstance(verdict, str) and 0.0 <= score <= 1.0
                assert rules == sorted(set(rules))


def test_the_baseline_holds_every_project_of_both_dbt_rounds(baseline):
    assert {name: len(projects) for name, projects in baseline["realworld"].items()} == ROUNDS
    for projects in baseline["realworld"].values():
        for project in projects.values():
            assert project["status"] in {"checked", "not_applicable"}
            for model in project.get("models", {}).values():
                assert model["verdict"] in {"clean", "leaky", "unverifiable", "error"}


def test_the_baseline_is_written_in_the_form_the_snapshot_writes(baseline):
    assert BASELINE.read_text(encoding="utf-8") == snapshot.dumps(baseline)


@pytest.mark.skip(reason="enabled when the v4 rules are compared with the v3 baseline")
def test_the_verdicts_of_the_current_rules_equal_the_baseline(baseline):
    warehouse = snapshot.ROOT / ".warehouse" / "bench"
    now = snapshot.leakage_verdicts(42, warehouse)
    changed = {
        (split, case_id): (now[split][case_id], was)
        for split, cases in baseline["leakage"].items()
        for case_id, was in cases.items()
        if now[split][case_id] != was
    }
    assert not changed
