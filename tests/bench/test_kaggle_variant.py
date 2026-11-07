"""The spec rewrite of the secondary run in bench/kaggle/evaluate.py (`--variant`)."""

import copy
from pathlib import Path

import pytest
import yaml

from bench.kaggle import evaluate as ev
from bench.kaggle.evaluate import ModeResult, Query, Record

ROOT = Path(__file__).resolve().parents[2]
PREREG = yaml.safe_load((ROOT / "bench/kaggle/eval-prereg.yaml").read_text())
CASE_IDS = PREREG["inputs"]["cases"]
PLACEHOLDER = {"unused_placeholder_events": "occurred_at"}
LOADABLE = ["kl04", "kl05", "kl08", "kl09", "kl10", "kl11", "kl12", "kl14", "kl15", "kl17"]


def case_spec(case):
    return yaml.safe_load((ev.CASES_DIR / case / "spec.yaml").read_text())


def test_the_label_table_is_replaced_by_the_placeholder():
    spec = {"label_table": "Train", "time_columns": {"train": "ts"}, "boundary": "inclusive"}
    assert ev.variant_spec(spec) == {
        "label_table": "Train",
        "time_columns": PLACEHOLDER,
        "boundary": "inclusive",
    }


def test_another_temporal_table_stays_and_no_placeholder_is_added():
    spec = {"label_table": "labels", "time_columns": {"labels": "ts", "events": "event_ts"}}
    assert ev.variant_spec(spec)["time_columns"] == {"events": "event_ts"}


def test_a_spec_without_time_columns_gets_the_placeholder():
    assert ev.variant_spec({"label_table": "t"})["time_columns"] == PLACEHOLDER


def test_the_input_is_not_modified_and_nothing_else_changes():
    spec = {
        "label_table": "t",
        "entity_keys": [],
        "label_known_after": "0 seconds",
        "static_tables": ["p"],
        "time_columns": {"t": "ts"},
    }
    before = copy.deepcopy(spec)
    rewritten = ev.variant_spec(spec)
    assert spec == before
    assert {k: v for k, v in rewritten.items() if k != "time_columns"} == {
        k: v for k, v in before.items() if k != "time_columns"
    }


@pytest.mark.parametrize("case", CASE_IDS)
def test_only_time_columns_differ_for_every_case(case):
    spec = case_spec(case)
    rewritten = ev.variant_spec(spec)
    assert {k: v for k, v in rewritten.items() if k != "time_columns"} == {
        k: v for k, v in spec.items() if k != "time_columns"
    }
    assert rewritten["time_columns"] == PLACEHOLDER
    assert yaml.safe_load(yaml.safe_dump(rewritten, sort_keys=False)) == rewritten


def test_every_case_spec_loads_as_authored():
    assert ev.loadable_cases({case: case_spec(case) for case in CASE_IDS}) == CASE_IDS


def test_every_case_still_loads_after_the_rewrite():
    specs = {case: ev.variant_spec(case_spec(case)) for case in CASE_IDS}
    assert ev.loadable_cases(specs) == CASE_IDS
    registered = PREREG["variant_spec_workaround"]["metrics"]
    assert all(case in registered for case in LOADABLE)


def test_primary_stem_ignores_variant_results(tmp_path):
    for name in ("20261107-aaaaaaa.json", "20261107-bbbbbbb-variant.json", "20261105-ccccccc.json"):
        (tmp_path / name).write_text("{}")
    assert ev.primary_stem(tmp_path) == "20261107-aaaaaaa"
    assert ev.primary_stem(tmp_path / "missing") == "(none found)"


def test_variant_report_leads_with_its_label_and_adds_the_loadable_tables():
    leaky = ModeResult("leaky", 0.9, ("L1",))
    clean = ModeResult("clean", 0.0)
    err = ModeResult("error", detail="error: x")
    thresholds = {"static": 0.425, "replay": 0.004, "combined": 0.475}
    records = [
        Record(Query("kl04", "l.sql", "leaky", "L1", (), 1, False), leaky, leaky),
        Record(Query("kl04", "c.sql", "clean", None, (), 1, False), clean, clean),
        Record(Query("kl03", "l.sql", "leaky", "L1", (), 1, False), err, err),
        Record(Query("kl03", "c.sql", "clean", None, (), 1, False), err, err),
    ]
    kept = [r for r in records if r.query.case == "kl04"]
    result = {
        "meta": {
            "stem": "20261107-abcdef0-variant",
            "git_sha": "abcdef0",
            "git_dirty": False,
            "prereg_commit": "1234567",
            "variant_prereg_commit": "7654321",
            "cases_digest": "0" * 64,
            "checker": {"release_tag": "v0.2.0", "src_tree": "1" * 40},
            "thresholds": thresholds,
            "verdict_threshold": 0.5,
            "resamples": 50,
            "seed": 42,
        },
        "variant": {"primary_stem": "20261107-1111111", "loadable": ["kl04"], "rejected": ["kl03"]},
        "all": ev.summarize(records, thresholds, 42, 50, 0.95),
        "without_disputed": ev.summarize(records, thresholds, 42, 50, 0.95),
        "loadable": ev.summarize(kept, thresholds, 42, 50, 0.95),
        "loadable_without_disputed": ev.summarize(kept, thresholds, 42, 50, 0.95),
        "per_case": ev.per_case(records, thresholds),
        "problems": ev.problem_groups(records),
        "build": {
            c: {"status": "built", "reason": "", "rows": {"t": 1}, "commits": {"t": 1}}
            for c in ("kl03", "kl04")
        },
    }
    text = ev.render_markdown(result)
    assert text.startswith("# Kaggle leak cases: frozen checker v0.2.0, secondary variant")
    assert "Secondary run, not an evaluation of the cases as authored" in text
    assert "`20261107-1111111`" in text
    assert "Specs the checker still rejects: kl03" in text
    assert "## The 1 cases whose spec loads" in text
