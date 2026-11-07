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


def test_clopper_pearson_matches_the_published_intervals():
    from bench.leakage.metrics import clopper_pearson

    assert clopper_pearson(0, 29) == (0.0, 0.1194)  # zero false positives in 29 templates
    assert clopper_pearson(10, 20) == (0.272, 0.728)
    assert clopper_pearson(1, 10) == (0.0025, 0.445)
    assert clopper_pearson(5, 5) == (0.4782, 1.0)
    assert clopper_pearson(0, 0)[0] != clopper_pearson(0, 0)[0]  # no templates: nan


def test_template_rates_count_templates_and_what_the_product_blocks():
    from bench.leakage.metrics import template_rates

    #            clean templates a(2 cases) b(2) c(1)   leaky d
    labels = [False, False, False, False, False, True]
    scores = [0.9, 0.0, 0.1, 0.0, 0.0, 1.0]
    groups = ["a", "a", "b", "b", "c", "d"]
    rates = template_rates(labels, scores, 0.5, groups, [False, False, False, True, False, False])
    assert rates["template_fpr"]["k"] == 1 and rates["template_fpr"]["n"] == 3
    # template b is not flagged, but one of its cases cannot be checked: it is blocked
    assert rates["block_rate_clean"]["k"] == 2 and rates["block_rate_clean"]["rate"] == 0.6667
    assert (rates["clean_cases"], rates["clean_cases_flagged"], rates["clean_cases_blocked"]) == (
        5,
        1,
        2,
    )
    assert rates["template_fpr"]["ci95"][0] < 1 / 3 < rates["template_fpr"]["ci95"][1]


# --- prevalence-aware and one-sided metrics -------------------------------------------------


def test_mcc_and_youden_j_by_hand():
    from bench.leakage.metrics import Confusion, mcc, youden_j

    c = Confusion(tp=6, fp=2, tn=8, fn=4)
    # (6*8 - 2*4) / sqrt(8 * 10 * 10 * 12)
    assert mcc(c) == pytest.approx(40 / 9600**0.5)
    assert youden_j(c) == pytest.approx(6 / 10 + 8 / 10 - 1)


def test_mcc_extremes_and_degenerate_tables():
    from bench.leakage.metrics import Confusion, mcc, youden_j

    assert mcc(Confusion(5, 0, 5, 0)) == 1.0
    assert mcc(Confusion(0, 5, 0, 5)) == -1.0
    assert mcc(Confusion(5, 5, 5, 5)) == 0.0
    # a margin is empty (nothing predicted positive): MCC is taken as 0, the usual convention
    assert mcc(Confusion(0, 0, 7, 3)) == 0.0
    assert mcc(Confusion(0, 0, 0, 0)) == 0.0
    assert youden_j(Confusion(5, 0, 5, 0)) == 1.0
    assert youden_j(Confusion(5, 5, 5, 5)) == 0.0
    # no clean cases: specificity cannot be measured, so J is 0 rather than a half-defined number
    assert youden_j(Confusion(3, 0, 0, 1)) == 0.0


def test_mcc_and_youden_j_reproduce_the_published_template_level_numbers():
    from bench.leakage.metrics import Confusion, mcc, youden_j

    # holdout_v3 combined, by template: 76 leaky found, none missed, 10 of 70 clean flagged
    c = Confusion(tp=76, fp=10, tn=60, fn=0)
    assert round(mcc(c), 2) == 0.87
    assert round(youden_j(c), 2) == 0.86


def test_published_holdout_v3_report_gives_the_same_scores_from_its_json():
    import json
    from pathlib import Path

    from bench.leakage.metrics import Confusion, mcc, precision_at_prevalence, youden_j

    path = Path(__file__).parents[2] / "results/leakage/20261106-4587293.json"
    combined = json.loads(path.read_text())["splits"]["holdout_v3"]["detectors"]["combined"]
    c = Confusion(combined["tp"], combined["fp"], combined["tn"], combined["fn"])
    assert (c.tp, c.fp, c.tn, c.fn) == (110, 17, 94, 0)
    assert mcc(c) == pytest.approx(110 * 94 / (127 * 110 * 94 * 111) ** 0.5)
    assert youden_j(c) == pytest.approx(1 - 17 / 111)
    # at a deployment prevalence of 5% the same detector is wrong far more often than at 50%
    assert precision_at_prevalence(c.recall, c.fpr, 0.05) == pytest.approx(
        0.05 / (0.05 + 0.95 * 17 / 111)
    )
    template = combined["template_fpr"]
    assert (template["k"], template["n"]) == (10, 70)
    assert precision_at_prevalence(1.0, template["k"] / template["n"], 0.05) == pytest.approx(
        0.05 / (0.05 + 0.95 * 10 / 70)
    )
    assert round(precision_at_prevalence(1.0, 10 / 70, 0.05), 2) == 0.27


def test_precision_at_prevalence_by_hand():
    from bench.leakage.metrics import DEPLOYMENT_PREVALENCES, precision_at_prevalence

    assert DEPLOYMENT_PREVALENCES == (0.01, 0.05, 0.10, 0.20)
    # tpr 0.9, fpr 0.1 at pi = 0.5 collapses to tpr / (tpr + fpr)
    assert precision_at_prevalence(0.9, 0.1, 0.5) == pytest.approx(0.9)
    assert precision_at_prevalence(0.9, 0.1, 0.1) == pytest.approx(0.09 / (0.09 + 0.09))
    assert precision_at_prevalence(1.0, 0.0, 0.01) == 1.0  # no false alarms: always right
    assert precision_at_prevalence(0.0, 0.2, 0.1) == 0.0
    assert precision_at_prevalence(0.0, 0.0, 0.1) == 0.0  # flags nothing: no precision to speak of
    # monotone: the rarer the leak, the lower the precision
    values = [precision_at_prevalence(0.95, 0.05, pi) for pi in DEPLOYMENT_PREVALENCES]
    assert values == sorted(values)


@pytest.mark.parametrize(("tpr", "fpr", "pi"), [(-0.1, 0.1, 0.1), (1.1, 0.1, 0.1), (0.5, 1.5, 0.1)])
def test_precision_at_prevalence_rejects_rates_outside_the_unit_interval(tpr, fpr, pi):
    from bench.leakage.metrics import precision_at_prevalence

    with pytest.raises(ValueError):
        precision_at_prevalence(tpr, fpr, pi)


@pytest.mark.parametrize("pi", [-0.01, 1.01, float("nan")])
def test_precision_at_prevalence_rejects_a_prevalence_outside_the_unit_interval(pi):
    from bench.leakage.metrics import precision_at_prevalence

    with pytest.raises(ValueError):
        precision_at_prevalence(0.9, 0.1, pi)


def test_auprc_matches_the_average_precision_textbook_example():
    from bench.leakage.metrics import auprc

    # the worked example from the scikit-learn documentation: AP = 0.5 * 1 + 0.5 * 2/3
    assert auprc([False, False, True, True], [0.1, 0.4, 0.35, 0.8]) == pytest.approx(5 / 6)


def test_auprc_on_the_roc_fixture_by_hand():
    from bench.leakage.metrics import auprc

    # ranked: 0.9+ 0.8+ 0.7- 0.3+ 0.2- 0.1- 0.0-  -> precision at each hit: 1, 1, 3/4
    assert auprc(LABELS, SCORES) == pytest.approx((1 + 1 + 3 / 4) / 3)


def test_auprc_perfect_ties_and_degenerate_cases():
    from bench.leakage.metrics import auprc

    assert auprc([True, True, False, False], [0.9, 0.8, 0.2, 0.1]) == 1.0
    # all scores tied: one threshold, precision = prevalence at full recall
    assert auprc([True, False, False, False], [0.5, 0.5, 0.5, 0.5]) == pytest.approx(0.25)
    # the worst ranking: the positive comes last behind three negatives
    assert auprc([False, False, False, True], [0.9, 0.8, 0.7, 0.1]) == pytest.approx(0.25)
    # ties are grouped, so the order of tied cases does not matter
    a = auprc([True, False, True, False], [0.5, 0.5, 0.1, 0.1])
    b = auprc([False, True, False, True], [0.5, 0.5, 0.1, 0.1])
    assert a == b == pytest.approx(0.5)
    no_positive = auprc([False, False], [0.3, 0.4])
    assert no_positive != no_positive  # undefined without a positive: nan


def test_auprc_rejects_mismatched_inputs():
    from bench.leakage.metrics import auprc

    with pytest.raises(ValueError):
        auprc([True, False], [0.5])


def test_one_sided_clopper_pearson_has_closed_forms_at_the_edges():
    from bench.leakage.metrics import clopper_pearson_one_sided

    # zero events in n: upper = 1 - alpha^(1/n) ("rule of three" for large n)
    assert clopper_pearson_one_sided(0, 29) == pytest.approx(1 - 0.05 ** (1 / 29))
    assert clopper_pearson_one_sided(0, 29) == pytest.approx(3 / 29, rel=0.1)
    # all events in n: lower = alpha^(1/n)
    assert clopper_pearson_one_sided(10, 10, side="lower") == pytest.approx(0.05 ** (1 / 10))
    assert clopper_pearson_one_sided(0, 10, side="lower") == 0.0
    assert clopper_pearson_one_sided(10, 10, side="upper") == 1.0


def test_one_sided_clopper_pearson_matches_the_reported_bounds():
    from bench.leakage.metrics import clopper_pearson_one_sided as upper

    assert round(upper(0, 70), 3) == 0.042
    assert round(upper(1, 70), 3) == 0.066
    assert round(upper(2, 70), 3) == 0.087
    assert round(upper(0, 64), 3) == 0.046
    assert round(upper(2, 30), 3) == 0.195
    assert round(upper(3, 30), 3) == 0.239


def test_one_sided_bound_is_tighter_than_the_two_sided_one_at_the_same_level():
    from bench.leakage.metrics import clopper_pearson, clopper_pearson_one_sided

    lo, hi = clopper_pearson(3, 20)
    assert clopper_pearson_one_sided(3, 20, side="upper") < hi
    assert clopper_pearson_one_sided(3, 20, side="lower") > lo
    # a one-sided 95% bound is the upper end of a two-sided 90% interval
    assert clopper_pearson_one_sided(3, 20, level=0.95) == pytest.approx(
        clopper_pearson(3, 20, level=0.90)[1], abs=1e-4
    )
    assert clopper_pearson_one_sided(3, 20, level=0.95, side="lower") == pytest.approx(
        clopper_pearson(3, 20, level=0.90)[0], abs=1e-4
    )


@pytest.mark.parametrize(("errors", "smallest_n"), [(0, 59), (1, 93), (2, 124), (3, 153)])
def test_the_sample_sizes_that_can_prove_a_false_positive_rate_of_five_percent(errors, smallest_n):
    from bench.leakage.metrics import clopper_pearson_one_sided as upper

    # the exact bound is compared unrounded: 1 in 93 sits at 0.04999, 1 in 92 at 0.0505
    assert upper(errors, smallest_n) <= 0.05
    assert upper(errors, smallest_n - 1) > 0.05


def test_one_sided_bounds_are_monotone_and_enclose_the_estimate():
    from bench.leakage.metrics import clopper_pearson_one_sided as bound

    uppers = [bound(k, 50) for k in range(0, 51)]
    assert uppers == sorted(uppers)
    for k in range(1, 50):
        assert bound(k, 50, side="lower") < k / 50 < bound(k, 50, side="upper")


def test_one_sided_clopper_pearson_rejects_bad_arguments():
    from bench.leakage.metrics import clopper_pearson_one_sided as bound

    assert bound(0, 0) != bound(0, 0)  # no units: nan, as for the two-sided interval
    with pytest.raises(ValueError):
        bound(1, 10, side="both")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        bound(11, 10)
    with pytest.raises(ValueError):
        bound(-1, 10)
    with pytest.raises(ValueError):
        bound(1, 10, level=1.0)
    with pytest.raises(ValueError):
        bound(1, 10, level=0.0)
