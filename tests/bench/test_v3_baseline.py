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


UNCHANGED_SPLITS = ("dev", "dev_v1b", "holdout_v2", "real")


@pytest.fixture(scope="module")
def now():
    warehouse = snapshot.ROOT / ".warehouse" / "bench"
    return snapshot.leakage_verdicts(42, warehouse)


def verdicts(reading):
    return {mode: reading[mode][0] for mode in ("static", "combined")}


@pytest.mark.parametrize("split", UNCHANGED_SPLITS)
def test_the_verdicts_of_the_splits_without_a_known_fix_have_not_changed(baseline, now, split):
    changed = {
        case_id: (verdicts(was), verdicts(now[split][case_id]))
        for case_id, was in baseline["leakage"][split].items()
        if verdicts(now[split][case_id]) != verdicts(was)
    }
    assert not changed


def test_no_leaky_template_of_holdout_v3_lost_its_verdict_and_no_clean_one_gained_a_flag(
    baseline, now
):
    lost, gained = [], []
    for case_id, was in baseline["leakage"]["holdout_v3"].items():
        before, after = verdicts(was), verdicts(now["holdout_v3"][case_id])
        for mode in before:
            if "_clean_" in case_id:
                if before[mode] != "leaky" and after[mode] == "leaky":
                    gained.append((case_id, mode))
            elif before[mode] == "leaky" and after[mode] != "leaky":
                lost.append((case_id, mode))
    assert not lost and not gained


def test_only_holdout_v3_changed_the_rule_ids_it_reports(baseline, now):
    """The rule ids of the other splits are the ones the v3 rules reported."""
    for split in UNCHANGED_SPLITS:
        for case_id, was in baseline["leakage"][split].items():
            assert {m: was[m][2] for m in was} == {m: now[split][case_id][m][2] for m in was}, (
                case_id
            )
