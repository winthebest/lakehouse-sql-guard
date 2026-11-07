"""The extra numbers of a development run, by hand."""

import pytest

from bench.kaggle import dev_report
from bench.leakage.metrics import Confusion


def test_two_layer_numbers_of_a_confusion():
    got = dev_report.two_layer(Confusion(tp=9, fp=1, tn=9, fn=1))
    assert (got["recall"], got["fpr"]) == (0.9, 0.1)
    assert got["mcc"] == pytest.approx(0.8)
    assert got["youden_j"] == pytest.approx(0.8)
    assert got["precision_at_prevalence"]["10%"] == pytest.approx(
        0.9 * 0.1 / (0.9 * 0.1 + 0.1 * 0.9)
    )
    assert (
        0.1 < got["fpr_upper_95_one_sided"] < 0.4 and 0.6 < got["recall_lower_95_one_sided"] < 0.9
    )


def row(case, name, label, cls, mode="static", **extra):
    base = {
        "case": case,
        "file": name,
        "label": label,
        "mode": mode,
        "class": cls,
        "replay_status": "",
        "rows_compared": "0",
        "rows_mismatched": "0",
    }
    return base | extra


def test_a_pair_is_told_apart_when_the_leaky_side_is_flagged_and_the_clean_side_is_not():
    rows = [
        row("k1", "leaky_1.sql", "leaky", "leaky"),
        row("k1", "clean_1.sql", "clean", "clean"),
        row("k1", "leaky_2.sql", "leaky", "leaky"),
        row("k1", "clean_2.sql", "clean", "leaky"),
        row("k2", "leaky_1.sql", "leaky", "clean"),
        row("k2", "clean_1.sql", "clean", "clean"),
    ]
    got = dev_report.pairs_told_apart(rows)
    assert got == {"told_apart": 1, "pairs": 3, "not_told_apart": ["k1/2", "k2/1"]}


def test_the_replay_score_is_the_mismatch_rate_and_feeds_the_auprc():
    rows = []
    for i, (label, cls, status, compared, bad) in enumerate(
        [("leaky", "leaky", "mismatch", 10, 4), ("clean", "clean", "pass", 10, 0)]
    ):
        rows.append(
            row(
                "k",
                f"{label}_{i}.sql",
                label,
                cls,
                "replay",
                replay_status=status,
                rows_compared=str(compared),
                rows_mismatched=str(bad),
            )
        )
        rows.append(row("k", f"{label}_{i}.sql", label, cls))
    got = dev_report.kaggle_report(rows)
    assert got["replay"]["auprc_replay_score"] == 1.0
    assert got["static"]["mcc"] == 1.0


def test_holdout_report_reads_the_detector_counts():
    result = {
        "splits": {
            "s": {
                "detectors": {
                    name: {
                        "tp": 8,
                        "fp": 1,
                        "tn": 9,
                        "fn": 2,
                        "template_fpr": {"k": 1, "n": 10, "rate": 0.1},
                    }
                    for name in ("static", "replay", "combined")
                }
            }
        }
    }
    got = dev_report.holdout_report(result, "s")
    assert set(got) == {"static", "replay", "combined"}
    assert got["combined"]["template_fpr"]["upper_95_one_sided"] > 0.1
