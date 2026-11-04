"""The fix-suggestion benchmark: rates on the labelled leaky cases, and no repair that silently
loses a row."""

from itertools import groupby

import pytest

from bench.leakage import fixes_eval
from bench.leakage.cases import LeakCase, generate_cases
from bench.leakage.run import SYNTHETIC_SPEC
from sqlguard.leakage.fixes import Fix, Verification
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
                "lost_rows_without_caveat": [],
                "not_fully_verified": ["t1"],
            }
        }
    }
    text = fixes_eval.markdown(result)
    assert "static_and_replay" in text and "in-sample" in text and "t1" in text
    assert "semantics preserved" in text and "semantics changed" in text


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
