"""Verdicts of earlier and new judges put on a run's findings."""

from bench.realworld import assemble


def finding(number, location="x", **cells):
    base = {
        "id": f"o/p#m#{number}",
        "project": "o/p",
        "training_model": "m",
        "rule": "L6",
        "location": location,
        "message": "msg",
        "controller": "",
        "reviewer": "",
        "final": "",
        "reason": "",
    }
    return base | cells


def test_old_verdicts_new_judgements_and_unjudged_rows_are_told_apart():
    old = [finding(7, "old", controller="true", reviewer="true", final="true", reason="r")]
    run = [finding(1, "old"), finding(2, "new"), finding(3, "new"), finding(4, "left")]
    judged = [finding(2, "new"), finding(3, "new")]
    a = {
        "o/p#m#2": {"id": "o/p#m#2", "label": "false", "reason": "a2"},
        "o/p#m#3": {"id": "o/p#m#3", "label": "true", "reason": "a3"},
    }
    b = {
        "o/p#m#2": {"id": "o/p#m#2", "label": "false", "reason": "b2"},
        "o/p#m#3": {"id": "o/p#m#3", "label": "false", "reason": "b3"},
    }
    done, pairs = assemble.assemble(run, old, judged, a, b)
    assert [r["final"] for r in done] == ["true", "false", "unsure", ""]
    assert [p["id"] for p in pairs] == ["o/p#m#2", "o/p#m#3"]
    # the second finding of one key takes the second verdict of that key
    assert done[1]["reason"] == "a2" and "A=true" in done[2]["reason"]


def test_metrics_count_the_unlabelled_and_the_agreement_of_the_new_pair():
    findings = [
        finding(1, controller="false", reviewer="false", final="false"),
        finding(2, controller="true", reviewer="false", final="unsure"),
        finding(3),
    ]
    models = [{"verdict": "leaky"}, {"verdict": "unverifiable"}]
    got = assemble.metrics(findings, models, {"o/p#m#1", "o/p#m#2"})
    assert got["findings"] == 3 and got["unlabelled"] == 1 and got["unsure"] == 1
    assert got["verdict_coverage"] == 0.5
    assert got["agreement_this_round"]["n"] == 2
    assert got["dev"] is True
