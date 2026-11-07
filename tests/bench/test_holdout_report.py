"""The report columns added for the held-out runs: prevalence-aware scores, one-sided exact
bounds at the unit the targets are set on (the template), and the per-split world."""

import pytest

from bench.leakage.cases import LeakCase
from bench.leakage.metrics import (
    Confusion,
    clopper_pearson_one_sided,
    mcc,
    template_confusion,
    youden_j,
)
from bench.leakage.run import DETECTORS, Scored, evaluate, markdown
from bench.leakage.splits import load_splits

#            clean templates a(2 cases) b(2) c(1)     leaky d(2) e(2) f(1)
LABELS = [False, False, False, False, False, True, True, True, True, True]
SCORES = [0.9, 0.0, 0.1, 0.0, 0.0, 1.0, 1.0, 1.0, 0.0, 1.0]
GROUPS = ["a", "a", "b", "b", "c", "d", "d", "e", "e", "f"]


def test_a_template_is_flagged_if_any_rendering_is_and_caught_only_if_every_rendering_is():
    c = template_confusion(LABELS, SCORES, 0.5, GROUPS)
    # clean: a flagged (one rendering), b and c not.
    # leaky: d caught, e only half caught (missed), f caught
    assert (c.tp, c.fn, c.fp, c.tn) == (2, 1, 1, 2)


def test_template_confusion_of_nothing_is_empty():
    assert template_confusion([], [], 0.5, []) == Confusion(0, 0, 0, 0)


def test_template_confusion_rejects_mismatched_lengths():
    with pytest.raises(ValueError):
        template_confusion([True], [0.5, 0.2], 0.5, ["a"])


def _scored(labels, scores, groups):
    cases = [
        LeakCase(
            f"{g}-{i}",
            g,
            "holdout",
            "leaky" if y else "clean",
            "L1" if y else "clean",
            {},
            "SELECT 1",
        )
        for i, (y, g) in enumerate(zip(labels, groups, strict=True))
    ]
    return [
        Scored(c, {d: s for d in DETECTORS}, [], {d: 0.0 for d in DETECTORS})
        for c, s in zip(cases, scores, strict=True)
    ]


def test_evaluate_adds_the_prevalence_aware_columns_next_to_recall_and_fpr():
    report = evaluate(_scored(LABELS, SCORES, GROUPS), {d: 0.5 for d in DETECTORS})
    combined = report["detectors"]["combined"]
    cases = Confusion(tp=4, fp=1, tn=4, fn=1)
    assert combined["recall"] == 0.8 and combined["fpr"] == 0.2  # unchanged columns
    assert combined["mcc"] == pytest.approx(round(mcc(cases), 4))
    assert combined["youden_j"] == pytest.approx(round(youden_j(cases), 4))
    assert combined["auprc"] == pytest.approx(1.0, abs=0.15)
    assert set(combined["precision_at_prevalence"]) == {"0.01", "0.05", "0.1", "0.2"}
    p5 = combined["precision_at_prevalence"]["0.05"]
    assert p5 == pytest.approx(round(0.8 * 0.05 / (0.8 * 0.05 + 0.2 * 0.95), 4))


def test_evaluate_reports_the_template_level_one_sided_bounds():
    report = evaluate(_scored(LABELS, SCORES, GROUPS), {d: 0.5 for d in DETECTORS})
    t = report["detectors"]["combined"]["templates"]
    assert (t["tp"], t["fn"], t["fp"], t["tn"]) == (2, 1, 1, 2)
    assert t["fpr"] == pytest.approx(1 / 3, abs=1e-4) and t["recall"] == pytest.approx(
        2 / 3, abs=1e-4
    )
    assert t["fpr_upper95"] == pytest.approx(clopper_pearson_one_sided(1, 3), abs=1e-4)
    assert t["recall_lower95"] == pytest.approx(
        clopper_pearson_one_sided(2, 3, side="lower"), abs=1e-4
    )
    assert t["mcc"] == pytest.approx(round(mcc(Confusion(2, 1, 2, 1)), 4))
    assert t["youden_j"] == pytest.approx(round(youden_j(Confusion(2, 1, 2, 1)), 4))
    assert set(t["precision_at_prevalence"]) == {"0.01", "0.05", "0.1", "0.2"}
    assert t["precision_lower95"] == pytest.approx(
        clopper_pearson_one_sided(2, 3, side="lower"), abs=1e-4
    )


def test_a_split_without_clean_or_leaky_templates_reports_no_bound_not_a_crash():
    report = evaluate(_scored([True, True], [1.0, 1.0], ["a", "b"]), {d: 0.5 for d in DETECTORS})
    t = report["detectors"]["combined"]["templates"]
    assert t["fpr_upper95"] is None  # no clean template: nothing to bound
    assert t["recall_lower95"] == pytest.approx(
        clopper_pearson_one_sided(2, 2, side="lower"), abs=1e-4
    )


def _result(split):
    return {
        "git_sha": "abc",
        "prereg_sha": "abc",
        "splits": {"holdout": split},
        "in_sample_splits": [],
        "real_data": None,
        "optimism_gap": {},
    }


def test_markdown_shows_the_new_columns_and_how_to_read_them():
    split = evaluate(_scored(LABELS, SCORES, GROUPS), {d: 0.5 for d in DETECTORS})
    text = markdown(_result(split))
    assert "MCC" in text and "Youden" in text and "AUPRC" in text
    assert "precision at prevalence" in text.lower()
    assert "1%" in text and "5%" in text and "20%" in text
    assert "one-sided 95%" in text
    assert "upper bound of the false positive rate" in text.lower()


def test_markdown_still_renders_a_result_written_before_these_columns_existed():
    split = evaluate(_scored(LABELS, SCORES, GROUPS), {d: 0.5 for d in DETECTORS})
    for m in split["detectors"].values():
        for key in ("mcc", "youden_j", "auprc", "precision_at_prevalence", "templates"):
            m.pop(key)
    text = markdown(_result(split))
    assert "## holdout" in text and "Youden" not in text


def test_the_manifest_world_defaults_to_the_churn_world():
    assert {s.world for s in load_splits().values()} == {"w1"}


def test_a_split_may_name_the_event_log_world_and_nothing_else(tmp_path):
    from bench.leakage.splits import load_splits as load

    (tmp_path / "s.yaml").write_text("x: 1\n")

    def manifest(entry: str):
        path = tmp_path / "splits.yaml"
        path.write_text(f"splits:\n  a:\n    spec: s.yaml\n    seen: false\n{entry}")
        return path

    assert load(manifest("    world: w2\n"))["a"].world == "w2"
    assert load(manifest(""))["a"].world == "w1"
    with pytest.raises(ValueError, match="world"):
        load(manifest("    world: w3\n"))
    with pytest.raises(ValueError, match="world"):
        load(manifest("    colour: red\n"))


def test_a_split_on_the_event_log_world_runs_on_that_world_without_the_v1_rows(
    tmp_path, monkeypatch, world
):
    from pathlib import Path

    from bench.leakage import run as bench_run
    from bench.leakage.splits import Split

    spec = Path("bench/leakage/spec_holdout_v4_w2.yaml").resolve()
    monkeypatch.setattr(bench_run, "load_splits", lambda: {"h": Split("h", spec, False, "w2")})
    one_case = LeakCase("t-000", "t", "h", "clean", "clean", {}, "SELECT 1 AS x FROM interactions")
    monkeypatch.setattr(bench_run, "generate_cases", lambda seed, splits: [one_case])
    seen = {}
    monkeypatch.setattr(
        bench_run,
        "score_cases",
        lambda cases, catalog, spec_, seed, **kw: (
            seen.update(tables=set(catalog.table_names()), **kw) or []
        ),
    )
    churn, _ = world
    bench_run.score_split("h", 7, tmp_path, churn, with_v1=True)
    assert "interactions" in seen["tables"] and "labels" not in seen["tables"]
    assert seen["v1_spec"] is None and seen["v1_scores"] is None


def test_the_roles_a_template_declares_reach_the_checker(world):
    """Wiring only: the header of a template changes the spec its query is checked with."""
    from bench.leakage.run import SYNTHETIC_SPEC, score_cases
    from sqlguard.leakage.spec import LeakageSpec

    catalog, _ = world
    spec = LeakageSpec.from_yaml(SYNTHETIC_SPEC)
    sql = (
        "SELECT l.user_id, l.label_ts, l.churned, "
        "LEAD(l.churned) OVER (PARTITION BY l.user_id ORDER BY l.label_ts) AS next_churned "
        "FROM labels l"
    )
    plain = LeakCase("a-000", "a", "h", "clean", "clean", {}, sql)
    declared = LeakCase(
        "b-000", "b", "h", "clean", "clean", {}, sql, output_roles={"label": ["next_churned"]}
    )
    by_id = {s.case.case_id: s for s in score_cases([plain, declared], catalog, spec, seed=1)}
    assert by_id["a-000"].scores["static"] >= 0.5  # reads the next label as a feature
    assert by_id["b-000"].scores["static"] == 0.0  # declared as the label it is
