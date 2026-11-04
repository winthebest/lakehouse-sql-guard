"""The fix-suggestion benchmark: rates on the labelled leaky cases, and no repair that silently
loses a row."""

from itertools import groupby

import pytest

from bench.leakage import fixes_eval
from bench.leakage.cases import LeakCase, generate_cases
from bench.leakage.run import SYNTHETIC_SPEC
from sqlguard.leakage.fixes import Fix, Semantics, Verification
from sqlguard.leakage.replay import Replayer
from sqlguard.leakage.spec import LeakageSpec


@pytest.fixture(scope="module")
def sample():
    """Two cases of every dev template: all the shapes, a fraction of the time."""
    cases = sorted(generate_cases(42, ("dev",)), key=lambda c: c.template_id)
    return [c for _, group in groupby(cases, key=lambda c: c.template_id) for c in list(group)[:2]]


def test_most_leaky_dev_cases_get_a_replayed_rewrite(world, sample):
    catalog, _ = world
    spec = LeakageSpec.from_yaml(SYNTHETIC_SPEC)
    result = fixes_eval.evaluate(sample, catalog, spec, seed=42)
    assert result["n_cases"] > 50
    assert result["rates"]["static_and_replay"] >= 0.8
    assert result["suggestion_rate"] >= result["rates"]["static_and_replay"]
    assert result["lost_rows_without_caveat"] == []
    # both paths: the label and the row-loss caveats do not depend on replay
    assert result["static_path"]["lost_rows_without_caveat"] == []
    assert result["semantics_differs"] == []
    # on current data no `preserved` fix changed which prediction rows come back, or how often
    assert result["preserved_but_rows_changed"] == []
    assert result["changed_rows_without_caveat"] == []
    assert result["static_path"]["rates"]["static_and_replay"] == 0  # nothing is replayed
    # the meaning of a rewrite is counted apart from its level
    assert result["semantics_counts"]["preserved"] > 0 and result["semantics_counts"]["changed"] > 0
    assert sum(result["semantics_counts"].values()) == result["n_cases"]
    # explained-only shapes stay explained-only
    assert {"l3_total_cte", "l3_unused_cte_sibling"} <= set(result["not_fully_verified"])


def test_l6_and_l7_cases_are_left_out_of_the_rates(world):
    catalog, _ = world
    spec = LeakageSpec.from_yaml(SYNTHETIC_SPEC)
    cases = [c for c in generate_cases(42, ("dev",)) if c.leak_type in ("L6", "L7")][:6]
    assert fixes_eval.evaluate(cases, catalog, spec, seed=42)["n_cases"] == 0


def test_the_report_names_the_levels():
    result = {
        "splits": {
            "dev": {
                "n_cases": 10,
                "suggestion_rate": 0.9,
                "rates": dict.fromkeys(fixes_eval.LEVELS, 0.25),
                "semantics_rates": dict.fromkeys(fixes_eval.SEMANTICS, 0.3),
                "level_by_semantics": {
                    m: dict.fromkeys(fixes_eval.LEVELS, 1) for m in fixes_eval.SEMANTICS
                },
                "by_type": {"L1": dict.fromkeys(fixes_eval.LEVELS, 1)},
                "static_path": {
                    "counts": dict.fromkeys(fixes_eval.LEVELS, 2),
                    "semantics_counts": dict.fromkeys(fixes_eval.SEMANTICS, 3),
                },
                "semantics_differs": [],
                "changed_rows_without_caveat": [],
                "preserved_but_rows_changed": [],
                "lost_rows_without_caveat": [],
                "not_fully_verified": ["t1"],
            }
        }
    }
    text = fixes_eval.markdown(result)
    assert "static_and_replay" in text and "in-sample" in text and "t1" in text
    assert "semantics preserved" in text and "semantics changed" in text
    assert "Without replay" in text and "Semantics differs between the paths: none" in text


def test_a_row_loss_needs_a_caveat_about_rows_not_any_caveat(world, monkeypatch):
    catalog, _ = world
    spec = LeakageSpec.from_yaml(SYNTHETIC_SPEC)
    replayer = Replayer(catalog, spec, seed=42)
    case = LeakCase(
        "c", "t", "dev", "leaky", "L3", {}, "SELECT l.user_id, l.label_ts, l.churned FROM labels l"
    )
    dropping = "SELECT l.user_id, l.label_ts, l.churned FROM labels l WHERE l.user_id < 3"

    def fake(caveat):
        return lambda *a, **k: [
            Fix("L3", "labels", case.sql, dropping, "x", [caveat], Verification.STATIC_ONLY)
        ]

    monkeypatch.setattr(fixes_eval, "suggest_fix", fake("the window is mirrored into the past"))
    assert fixes_eval.evaluate_case(case, catalog, spec, replayer)["lost_rows_without_caveat"]
    monkeypatch.setattr(fixes_eval, "suggest_fix", fake("rows may drop: a WHERE reads e"))
    assert not fixes_eval.evaluate_case(case, catalog, spec, replayer)["lost_rows_without_caveat"]


def test_a_drop_that_only_the_replay_path_warns_about_fails_the_static_path(world, monkeypatch):
    """Replay's row-count caveat must not be what makes the check pass."""
    catalog, _ = world
    spec = LeakageSpec.from_yaml(SYNTHETIC_SPEC)
    replayer = Replayer(catalog, spec, seed=42)
    case = LeakCase(
        "c", "t", "dev", "leaky", "L3", {}, "SELECT l.user_id, l.label_ts, l.churned FROM labels l"
    )
    dropping = "SELECT l.user_id, l.label_ts, l.churned FROM labels l WHERE l.user_id < 3"

    def fake(sql, spec, report=None, **kwargs):
        caveats = ["rows may drop: on the current data ..."] if kwargs.get("replay") else []
        return [Fix("L3", "labels", sql, dropping, "x", caveats, Verification.STATIC_ONLY)]

    monkeypatch.setattr(fixes_eval, "suggest_fix", fake)
    record = fixes_eval.evaluate_case(case, catalog, spec, replayer)
    assert not record["lost_rows_without_caveat"]
    assert record["static"]["lost_rows_without_caveat"]


def test_a_preserved_fix_that_changes_the_rows_fails_and_so_does_a_change_without_a_caveat(
    world, monkeypatch
):
    catalog, _ = world
    spec = LeakageSpec.from_yaml(SYNTHETIC_SPEC)
    replayer = Replayer(catalog, spec, seed=42)
    case = LeakCase(
        "c", "t", "dev", "leaky", "L3", {}, "SELECT l.user_id, l.label_ts, l.churned FROM labels l"
    )
    # same number of rows, other rows: the count cannot see it, the key set does
    swapped = "SELECT l.user_id + 1000 AS user_id, l.label_ts, l.churned FROM labels l"

    def fake(semantics, caveats):
        return lambda *a, **k: [
            Fix(
                "L3", "labels", case.sql, swapped, "x", caveats, Verification.STATIC_ONLY, semantics
            )
        ]

    monkeypatch.setattr(fixes_eval, "suggest_fix", fake(Semantics.PRESERVED, []))
    record = fixes_eval.evaluate_case(case, catalog, spec, replayer)
    assert record["static"]["preserved_but_rows_changed"] and record["preserved_but_rows_changed"]
    assert record["static"]["lost_rows_without_caveat"]
    monkeypatch.setattr(fixes_eval, "suggest_fix", fake(Semantics.CHANGED, ["rows may drop: x"]))
    record = fixes_eval.evaluate_case(case, catalog, spec, replayer)
    assert not record["static"]["preserved_but_rows_changed"]
    assert not record["static"]["lost_rows_without_caveat"]
