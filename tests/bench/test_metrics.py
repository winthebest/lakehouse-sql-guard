import pytest

from bench.leakage.baseline import baseline_score
from bench.leakage.metrics import auc, confusion, roc_curve, threshold_at_fpr
from sqlguard.core.duck import connect
from sqlguard.leakage.spec import LeakageSpec

LABELS = [True, True, True, False, False, False, False]
SCORES = [0.9, 0.8, 0.3, 0.7, 0.2, 0.1, 0.0]


def test_confusion_by_hand():
    c = confusion(LABELS, SCORES, 0.5)
    assert (c.tp, c.fp, c.tn, c.fn) == (2, 1, 3, 1)
    assert c.recall == pytest.approx(2 / 3)
    assert c.fpr == pytest.approx(1 / 4)
    assert c.precision == pytest.approx(2 / 3)


def test_roc_and_auc_by_hand():
    points = roc_curve(LABELS, SCORES)
    assert points[0][1:] == (0.0, 0.0) and points[-1][1:] == (1.0, 1.0)
    # 12 positive/negative pairs, 11 ranked correctly except (0.3 vs 0.7)
    assert auc(points) == pytest.approx(11 / 12)


def test_perfect_and_random_auc():
    assert auc(roc_curve([True, False], [1.0, 0.0])) == 1.0
    assert auc(roc_curve([True, False], [0.5, 0.5])) == 0.5


def test_threshold_at_fpr():
    assert threshold_at_fpr(LABELS, SCORES, 0.0) == pytest.approx(0.75)  # between 0.8 and 0.7
    assert threshold_at_fpr(LABELS, SCORES, 0.25) == pytest.approx(0.25)
    assert threshold_at_fpr([True, False], [0.0, 1.0], 0.0) == float("inf")
    assert threshold_at_fpr([True, True], [0.4, 0.9], 0.0) == 0.4


def test_baseline_only_sees_output_timestamps():
    from datetime import datetime

    spec = LeakageSpec.from_dict(
        {
            "label_table": "labels",
            "entity_keys": ["user_id"],
            "prediction_ts": "label_ts",
            "label_columns": ["y"],
            "time_columns": {"f": "ts"},
        }
    )
    import pyarrow as pa

    conn = connect(
        {
            "labels": pa.table({"user_id": [1, 2], "label_ts": [datetime(2026, 1, 2)] * 2}),
            "f": pa.table({"user_id": [1, 2], "ts": [datetime(2026, 1, 1), datetime(2026, 1, 3)]}),
        }
    )
    with_ts = "SELECT l.user_id, l.label_ts, f.ts FROM labels l JOIN f USING (user_id)"
    without_ts = "SELECT l.user_id, l.label_ts, f.user_id AS x FROM labels l JOIN f USING (user_id)"
    assert baseline_score(conn, with_ts, spec) == 0.5
    assert baseline_score(conn, without_ts, spec) == 0.0


def test_bootstrap_ci_resamples_whole_templates():
    from bench.leakage.metrics import bootstrap_ci

    labels = [True, True, True, True, False, False, False, False]
    scores = [0.9, 0.9, 0.1, 0.1, 0.0, 0.0, 0.9, 0.9]
    groups = ["a", "a", "b", "b", "c", "c", "d", "d"]
    ci = bootstrap_ci(labels, scores, 0.5, groups, n_resamples=500, seed=1)
    # Each template is either fully caught or fully missed, so recall only takes 0, 0.5, 1.
    assert ci["recall"][0] == 0.0 and ci["recall"][1] == 1.0
    assert ci["fpr"][0] == 0.0 and ci["fpr"][1] == 1.0
    assert ci == bootstrap_ci(labels, scores, 0.5, groups, n_resamples=500, seed=1)


def test_bootstrap_ci_is_tight_when_every_template_agrees():
    from bench.leakage.metrics import bootstrap_ci

    labels = [True, True, False, False]
    scores = [0.9, 0.8, 0.1, 0.0]
    ci = bootstrap_ci(labels, scores, 0.5, ["a", "b", "c", "d"], n_resamples=200)
    assert ci["recall"] == (1.0, 1.0) and ci["fpr"] == (0.0, 0.0)
