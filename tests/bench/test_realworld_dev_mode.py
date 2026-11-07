"""Development mode of the real-world runner: INFO findings counted apart, the freeze check
skipped on request, and agreement and alert-burden numbers."""

import json
import math

import pytest

from bench.realworld import agreement, rows
from bench.realworld import run as runner


def finding(severity="high", evidence=None):
    return {"severity": severity, "evidence": json.dumps(evidence) if evidence else ""}


def test_a_demoted_finding_is_info_only_when_the_checker_says_so():
    demoted = finding("info", {"non_feature_only": True, "outputs": ["churned"]})
    assert rows.is_info(demoted)
    assert not rows.is_info(finding("high", {"non_feature_only": True}))  # severity says scored
    assert not rows.is_info(finding("info", {"outputs": ["x"]}))
    assert not rows.is_info(finding("high"))
    assert not rows.is_info({"severity": "info", "evidence": "not json"})


def test_split_info_separates_the_two_kinds_and_keeps_order():
    a, b, c = finding(), finding("info", {"non_feature_only": True}), finding("medium")
    assert rows.split_info([a, b, c]) == ([a, c], [b])


def test_model_rows_carry_the_info_count():
    records = [
        {
            "repo": "o/p",
            "group": "round2",
            "status": "checked",
            "models": [
                {
                    "model": "m",
                    "verdict": "leaky",
                    "findings": 2,
                    "info_non_feature": 3,
                    "unchecked": 0,
                    "seconds": 1,
                }
            ],
        }
    ]
    row = rows.model_rows(records)[0]
    assert row["info_non_feature"] == 3 and "info_non_feature" in rows.MODEL_COLUMNS


def test_summary_counts_info_apart_from_findings():
    record = {
        "repo": "a/b",
        "group": "round2",
        "status": "checked",
        "models_in_project": 1,
        "training_models": 1,
        "findings": 2,
        "info_non_feature": 5,
        "unverifiable": 0,
        "errors": 0,
        "seconds": 0.5,
        "models": [{"verdict": "leaky", "unchecked": 0}],
    }
    summary = runner.summarise([record])["round2"]
    assert summary["findings"] == 2 and summary["info_non_feature"] == 5


def test_a_dev_run_skips_the_freeze_check_and_says_so(monkeypatch, tmp_path):
    def refuse(*args, **kwargs):
        raise rows.RealworldError("not frozen")

    monkeypatch.setattr(rows, "assert_rules_frozen", refuse)
    monkeypatch.setattr(runner, "load_projects", lambda path: [])
    with pytest.raises(rows.RealworldError):
        runner.main(["--out", str(tmp_path), "--full-csv", str(tmp_path / "f.csv")])
    assert (
        runner.main(["--dev", "--out", str(tmp_path), "--full-csv", str(tmp_path / "f.csv")]) == 0
    )
    (written,) = tmp_path.glob("2*.json")
    result = json.loads(written.read_text(encoding="utf-8"))
    assert result["dev"] is True and result["rules_tag"] is None and result["code_sha"]


def test_the_rules_tag_is_passed_to_the_freeze_check(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(
        rows, "assert_rules_frozen", lambda repo, tag=None, **k: seen.update(tag=tag)
    )
    monkeypatch.setattr(runner, "load_projects", lambda path: [])
    runner.main(
        [
            "--rules-tag",
            "rules-frozen-v4",
            "--out",
            str(tmp_path),
            "--full-csv",
            str(tmp_path / "f.csv"),
        ]
    )
    assert seen["tag"] == "rules-frozen-v4"
    result = json.loads(next(tmp_path.glob("2*.json")).read_text(encoding="utf-8"))
    assert result["dev"] is False and result["rules_tag"] == "rules-frozen-v4"


# --- agreement ---------------------------------------------------------------------------------


def test_kappa_and_ac1_by_hand():
    a = ["t", "t", "f", "f", "t", "f", "f", "f"]
    b = ["t", "f", "f", "f", "t", "f", "f", "t"]
    # po = 6/8; shares of a: t 3/8, f 5/8; of b: t 3/8, f 5/8 -> pe = 2 * 9/64... kappa
    pe = (3 * 3 + 5 * 5) / 64
    assert agreement.cohen_kappa(a, b) == pytest.approx((0.75 - pe) / (1 - pe))
    pi_t, pi_f = 6 / 16, 10 / 16
    pe_ac1 = (pi_t * (1 - pi_t) + pi_f * (1 - pi_f)) / 1
    assert agreement.gwet_ac1(a, b) == pytest.approx((0.75 - pe_ac1) / (1 - pe_ac1))


def test_ac1_stays_high_where_kappa_collapses_on_a_dominant_label():
    a = ["f"] * 28 + ["t", "t"]
    b = ["f"] * 28 + ["t", "f"]
    assert agreement.cohen_kappa(a, b) < 0.7 < 0.9 < agreement.gwet_ac1(a, b)


def test_perfect_and_degenerate_agreement():
    a = ["t", "f", "u"]
    assert agreement.cohen_kappa(a, a) == pytest.approx(1.0)
    assert agreement.gwet_ac1(a, a) == pytest.approx(1.0)
    assert agreement.cohen_kappa(["f", "f"], ["f", "f"]) is None
    assert agreement.gwet_ac1(["f", "f"], ["f", "f"]) is None


def test_unequal_or_empty_inputs_are_errors():
    with pytest.raises(ValueError):
        agreement.cohen_kappa(["a"], ["a", "b"])
    with pytest.raises(ValueError):
        agreement.gwet_ac1([], [])


def test_agreement_uses_only_rows_where_both_raters_labelled():
    data = [
        {"controller": "true", "reviewer": "true"},
        {"controller": "false", "reviewer": "false"},
        {"controller": "false", "reviewer": "true"},
        {"controller": "false", "reviewer": ""},
        {"controller": "", "reviewer": ""},
    ]
    got = agreement.agreement(data)
    assert got["n"] == 3 and got["observed"] == pytest.approx(0.6667, abs=1e-4)
    assert agreement.agreement([])["kappa"] is None


def test_burden_counts_strict_loose_effective_fp_and_coverage():
    findings = (
        [{"final": "true"}] * 5
        + [{"final": "false"}] * 24
        + [{"final": "unsure"}] * 4
        + [{"final": "true_not_actionable"}]
    )
    models = (
        [{"verdict": "leaky"}] * 4 + [{"verdict": "clean"}] * 2 + [{"verdict": "unverifiable"}] * 6
    )
    got = agreement.burden(findings, models)
    assert got["findings"] == 34 and got["true"] == 5
    assert got["precision_strict"] == pytest.approx(5 / 34, abs=1e-4)
    assert got["precision_loose"] == pytest.approx(9 / 34, abs=1e-4)
    assert got["effective_false_positive"] == pytest.approx(25 / 34, abs=1e-4)
    assert got["models_with_verdict"] == 6 and got["verdict_coverage"] == 0.5
    assert got["findings_per_model"] == pytest.approx(34 / 12, abs=1e-3)
    assert agreement.burden([], [])["precision_strict"] is None
    assert math.isclose(agreement.burden([], [])["findings"], 0)
