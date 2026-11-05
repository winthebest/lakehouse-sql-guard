"""The leakage benchmark counts and lists the cases a detector could not check."""

from dataclasses import replace

import pytest

from bench.leakage.cases import LEAK_TYPES, LeakCase
from bench.leakage.run import DETECTORS, SYNTHETIC_SPEC, evaluate, markdown, score_cases
from sqlguard.leakage.spec import LeakageSpec

CLEAN = (
    "SELECT l.user_id, l.label_ts, l.churned, s.total_spend FROM labels l "
    "ASOF JOIN user_stats s ON l.user_id = s.user_id AND l.label_ts > s.updated_at"
)
CRASH = "SELECT l.user_id, l.label_ts, l.churned, l.no_such_column FROM labels l"
EVENT_TIME_ONLY = (
    "SELECT l.user_id, l.label_ts, l.churned, SUM(e.amount) AS amt FROM labels l "
    "JOIN events e ON e.user_id = l.user_id AND e.event_ts < l.label_ts GROUP BY 1, 2, 3"
)


def case(name: str, sql: str, leak_type: str) -> LeakCase:
    label = "clean" if leak_type == "clean" else "leaky"
    return LeakCase(name, name, "dev", label, leak_type, {}, sql)


def test_unverifiable_cases_are_counted_and_listed(world):
    catalog, _ = world
    # without the declared load delay an event-time-only bound is a hint that only replay settles
    spec = replace(LeakageSpec.from_yaml(SYNTHETIC_SPEC), max_ingest_delay={})
    cases = [
        case("ok", CLEAN, "clean"),
        case("crash", CRASH, "clean"),
        case("hint", EVENT_TIME_ONLY, "L4"),
    ]
    scored = score_cases(cases, catalog, spec, seed=42)
    by_id = {s.case.case_id: s for s in scored}
    assert by_id["crash"].scores["static"] == 1.0  # an analysis crash still fails closed
    assert not by_id["ok"].verdicts

    report = evaluate(scored, {d: 0.5 for d in DETECTORS})
    assert report["detectors"]["static"]["n_unverifiable"] == 2  # crash + static-only L4 hint
    assert report["detectors"]["baseline"]["n_unverifiable"] == 0
    listed = {(u["case_id"], u["detector"]): u["reason"] for u in report["unverifiable_cases"]}
    assert "no_such_column" in listed[("crash", "static")]
    assert "settled by replay" in listed[("hint", "static")]
    assert ("ok", "static") not in listed and ("ok", "combined") not in listed

    declared = score_cases(
        [case("hint", EVENT_TIME_ONLY, "L4")], catalog, LeakageSpec.from_yaml(SYNTHETIC_SPEC), 42
    )
    assert declared[0].scores["static"] >= 0.5 and not declared[0].verdicts  # a finding, not a hint


def test_markdown_lists_unverifiable_cases(world):
    catalog, _ = world
    spec = LeakageSpec.from_yaml(SYNTHETIC_SPEC)
    scored = score_cases([case("crash", CRASH, "clean")], catalog, spec, seed=42)
    split = evaluate(scored, {d: 0.5 for d in DETECTORS})
    result = {
        "git_sha": "abc",
        "prereg_sha": "abc",
        "splits": {"dev": split},
        "in_sample_splits": ["dev"],
        "real_data": None,
        "optimism_gap": {},
    }
    text = markdown(result)
    assert "Unverifiable cases" in text and "`crash`" in text
    # the case fails closed: the clean template counts as blocked although its score is 1.0 anyway,
    # and the report gives exact template-level intervals next to the bootstrap
    rates = split["detectors"]["static"]
    assert rates["block_rate_clean"]["k"] == 1 and rates["block_rate_clean"]["n"] == 1
    assert "| clean templates | flagged / n [95% exact CI]" in text and "1/1 = 1.00" in text


def test_an_unverifiable_clean_case_is_blocked_without_being_flagged(world):
    """A detector that cannot check a clean query and says so has not flagged it, but the product
    blocks it: the block rate counts it, the false positive rate does not."""
    catalog, _ = world
    spec = LeakageSpec.from_yaml(SYNTHETIC_SPEC)
    scored = score_cases([case("ok", CLEAN, "clean")], catalog, spec, seed=42)
    scored[0].verdicts["static"] = "unverifiable"
    scored[0].reasons["static"] = "a table the spec does not declare"
    rates = evaluate(scored, {d: 0.5 for d in DETECTORS})["detectors"]["static"]
    assert rates["template_fpr"]["k"] == 0 and rates["block_rate_clean"]["k"] == 1


def split_stub() -> dict:
    detector = {
        "recall": 1.0,
        "fpr": 0.0,
        "precision": 1.0,
        "f1": 1.0,
        "auc": 1.0,
        "median_ms": None,
        "ci95": {},
        "recall_by_type": {},
        "n_unverifiable": 0,
    }
    return {
        "n_cases": 2,
        "n_leaky": 1,
        "detectors": {d: detector for d in DETECTORS},
        "attribution_accuracy": None,
        "unverifiable_cases": [],
    }


def result_for(*names: str, in_sample: tuple[str, ...] = ()) -> dict:
    return {
        "git_sha": "abc",
        "prereg_sha": "def",
        "splits": {name: split_stub() for name in names},
        "in_sample_splits": list(in_sample),
        "real_data": None,
        "optimism_gap": {},
    }


def test_report_says_which_splits_were_seen_during_development():
    text = markdown(result_for("dev", "dev_v1b", in_sample=("dev", "dev_v1b")))
    assert "In-sample:" in text and "dev, dev_v1b" in text
    assert "before held-out templates existed" not in text
    assert "## dev_v1b" in text and "holdout" not in text


def test_a_split_nobody_tuned_on_is_not_called_in_sample():
    text = markdown(result_for("dev", "holdout_v2", in_sample=("dev",)))
    assert (
        "**In-sample:** dev was seen" in text and "Not seen during development: holdout_v2" in text
    )


def test_run_needs_known_splits(tmp_path):
    from bench.leakage.cases import SPLITS
    from bench.leakage.run import main, run

    assert SPLITS == ("dev", "dev_v1b", "holdout_v2", "holdout_v3") and "L7" in LEAK_TYPES
    # The next held-out split has no templates until its authors hand them in.
    with pytest.raises(SystemExit, match="no templates for split"):
        run(42, tmp_path, False, tmp_path, splits=("holdout_v3",))
    with pytest.raises(SystemExit):
        main(["run", "--splits", "holdout"])


def test_bench_cli_takes_the_splits_to_score():
    from bench import cli

    with pytest.raises(SystemExit):
        cli.main(["leakage", "run", "--splits", "holdout"])


def test_fix_rates_by_verification_level_are_reported(world, monkeypatch):
    from bench.leakage import run as leakage_run
    from bench.leakage.cases import generate_cases

    catalog, _ = world
    few = [c for c in generate_cases(42, ("dev",)) if c.template_id in {"l1_future_window"}][:3]
    monkeypatch.setattr(leakage_run, "generate_cases", lambda seed, splits: few)
    rates = leakage_run._fix_rates("dev", 42, catalog)
    assert rates["n_cases"] == len(few)
    assert set(rates["rates"]) == {"static_and_replay", "static_only", "unverified", "none"}
    assert sum(rates["counts"].values()) == len(few)

    result = result_for("dev", in_sample=("dev",))
    result["fixes"] = {"dev": rates}
    text = markdown(result)
    assert "## Fix suggestions by verification level" in text
    assert "| dev | 3 |" in text and "static_and_replay" in text


def test_rows_without_a_threshold_are_not_reported():
    from bench.leakage.run import Scored, evaluate

    scored = [
        Scored(case("a", CLEAN, "clean"), {d: 0.0 for d in DETECTORS} | {"v1_static": 0.0}, [], {}),
        Scored(case("b", CRASH, "L2"), {d: 1.0 for d in DETECTORS} | {"v1_static": 1.0}, [], {}),
    ]
    report = evaluate(scored, {d: 0.5 for d in DETECTORS})
    assert "v1_static" not in report["detectors"]
    report = evaluate(scored, {d: 0.5 for d in (*DETECTORS, "v1_static")})
    assert report["detectors"]["v1_static"]["recall"] == 1.0


def test_row_a_unreadable_cases_are_broken_down_by_truth_with_the_raw_l7_result():
    from bench.leakage.run import Scored, evaluate
    from bench.leakage.v1_runner import V1Score, apply_v1_taxonomy

    def scored_case(name, truth, v1):
        v1 = apply_v1_taxonomy(v1, truth)
        scores = {d: 0.0 for d in DETECTORS} | {"v1_static": v1.static, "v1_combined": v1.combined}
        return Scored(case(name, "SELECT 1", truth), scores, [], {}, {}, {}, v1)

    unparsed = V1Score.not_detected("v1 cannot parse the query: x")
    seen = V1Score(0.9, 0.9, {}, ["L1"])
    scored = [
        scored_case("clean-read", "clean", V1Score(0.0, 0.0, {}, [])),
        scored_case("clean-unread", "clean", unparsed),
        scored_case("l1", "L1", seen),
        scored_case("l7-flagged", "L7", seen),
        scored_case("l7-missed", "L7", V1Score(0.1, 0.1, {}, [])),
    ]
    cut = {d: 0.5 for d in (*DETECTORS, "v1_static", "v1_combined")}
    reading = evaluate(scored, cut)["v1_reading"]
    assert reading["unreadable_by_truth"] == {"L7": 2, "clean": 1}
    assert reading["clean_credited_unread"] == 1
    assert {u["case_id"] for u in reading["unreadable_cases"]} == {
        "clean-unread",
        "l7-flagged",
        "l7-missed",
    }
    static = reading["v1_static"]
    assert static["recall"] == pytest.approx(1 / 3, abs=1e-3)  # L1 only: L7 is forced to 0
    assert static["recall_if_unnamed_types_were_read"] == pytest.approx(2 / 3, abs=1e-3)
    assert (static["forced_to_not_detected"], static["of_which_flagged_by_v1_raw"]) == (2, 1)

    split = {
        **split_stub(),
        "v1_reading": reading,
        "detectors": {"static": split_stub()["detectors"]["static"]},
    }
    text = markdown({**result_for("dev"), "splits": {"dev": split}, "in_sample_splits": ["dev"]})
    assert "could not read: L7 2, clean 1" in text and "v1 did not earn" in text
