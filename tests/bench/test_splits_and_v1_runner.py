"""Which splits are in-sample comes from data, and the v1 runner's contract with its worker."""

import json
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

from bench.leakage import splits, v1_runner
from bench.leakage.cases import DEFAULT_SPLITS, SPLITS
from bench.leakage.v1_runner import V1Score, apply_v1_taxonomy, run_v1, v1_spec_data

SPEC = Path("bench/leakage/spec.yaml")
CLEAN = (
    "SELECT l.user_id, l.label_ts, l.churned, s.total_spend FROM labels l "
    "ASOF JOIN user_stats s ON l.user_id = s.user_id AND l.label_ts > s.updated_at"
)


def warehouse_of(catalog) -> Path:
    return Path(catalog.catalog.properties["warehouse"].removeprefix("file://"))


# --- split manifest -------------------------------------------------------------------------


def test_the_manifest_marks_dev_splits_seen_and_holdout_v2_unseen():
    assert SPLITS == ("dev", "dev_v1b", "holdout_v2")
    assert DEFAULT_SPLITS == ("dev", "dev_v1b")
    assert splits.seen_splits() == {"dev", "dev_v1b"}
    manifest = splits.load_splits()
    assert manifest["holdout_v2"].spec.name == "spec_holdout_v2.yaml"
    assert manifest["dev"].spec == manifest["dev_v1b"].spec


def test_a_preregistration_can_only_add_to_what_was_seen():
    assert splits.seen_splits({"seen_splits": ["holdout_v2"]}) == {"dev", "dev_v1b", "holdout_v2"}
    assert splits.seen_splits({"seen_splits": []}) == {"dev", "dev_v1b"}  # old files lack it
    assert splits.seen_splits({"splits_seen": ["dev"]}) == {"dev", "dev_v1b"}


def test_a_broken_manifest_is_refused(tmp_path):
    path = tmp_path / "splits.yaml"
    for text, message in [
        ("splits: {}", "non-empty"),
        ("splits: {a: {spec: nope.yaml, seen: true}}", "not found"),
        ("splits: {a: {spec: s.yaml}}", "exactly"),
        ("splits: {a: {spec: s.yaml, seen: maybe}}", "true or false"),
    ]:
        path.write_text(text, encoding="utf-8")
        (tmp_path / "s.yaml").write_text("x: 1", encoding="utf-8")
        with pytest.raises(ValueError, match=message):
            splits.load_splits(path)


# --- v1 runner ------------------------------------------------------------------------------


def test_the_v1_spec_has_only_the_fields_v1_knew():
    data = yaml.safe_load(Path("bench/leakage/spec_holdout_v2.yaml").read_text(encoding="utf-8"))
    stripped = v1_spec_data(data)
    assert not set(stripped) & set(v1_runner.V2_SPEC_FIELDS)
    assert {"label_table", "time_columns", "availability_columns"} <= set(stripped)


def test_a_leak_type_v1_has_no_name_for_is_not_detected():
    score = V1Score(0.9, 0.95, {}, ["L2"])
    kept = apply_v1_taxonomy(score, "L3")
    assert kept is score
    gone = apply_v1_taxonomy(score, "L7")
    assert (gone.static, gone.combined, gone.unreadable) == (0.0, 0.0, True)
    assert (gone.raw_static, gone.raw_combined) == (0.9, 0.95)
    assert "L7" in gone.reasons["static"] and "L7" in gone.reasons["combined"]


def test_describe_unverifiable_reads_any_report_shape():
    class Finding:
        def __init__(self, rule_id, message):
            self.rule_id, self.message = rule_id, message

    class Report:
        def __init__(self, verdict, findings=(), replay=None):
            self.verdict, self.findings, self.replay = verdict, list(findings), replay

    assert v1_runner.describe_unverifiable(Report("clean"), {"PARSE"}) == ""
    report = Report("unverifiable", [Finding("PARSE", "bad"), Finding("L1", "x")])
    assert v1_runner.describe_unverifiable(report, {"PARSE"}) == "bad"
    assert "settled by replay" in v1_runner.describe_unverifiable(Report("unverifiable"), set())


def test_the_worker_scores_readable_queries_and_marks_the_rest_unreadable(world, tmp_path):
    """Protocol check with the current interpreter standing in for the v1 environment."""
    catalog, _ = world
    sqls = [CLEAN, "SELEC FROM WHERE", "SELECT no_such FROM nowhere"]
    scores = run_v1(
        sqls,
        spec_data=yaml.safe_load(SPEC.read_text(encoding="utf-8")),
        warehouse=warehouse_of(catalog),
        seed=42,
        python=Path(sys.executable),
        work_dir=tmp_path,
    )
    assert len(scores) == 3
    assert not scores[0].unreadable and scores[0].static < 0.2
    assert scores[1].unreadable and "parse" in scores[1].reasons["static"]
    assert scores[1].static == scores[1].combined == 0.0
    assert scores[2].static in (0.0, 1.0) or scores[2].unreadable


def test_a_worker_that_dies_costs_one_query_not_the_run(tmp_path):
    fake = tmp_path / "worker.py"
    fake.write_text(
        textwrap.dedent(
            """
            import json, os, sys
            args = sys.argv
            request = json.load(open(args[args.index("--in") + 1]))
            index = request["start"]
            with open(args[args.index("--out") + 1], "a") as out:
                out.write(json.dumps({"i": index, "static": 0.1, "combined": 0.1,
                                      "reasons": {}, "leak_types": []}) + "\\n")
            os._exit(9)
            """
        ),
        encoding="utf-8",
    )
    scores = run_v1(
        ["a", "b", "c", "d"],
        spec_data={},
        warehouse=tmp_path,
        seed=1,
        python=Path(sys.executable),
        work_dir=tmp_path / "work",
        worker=fake,
    )
    assert [s.unreadable for s in scores] == [False, True, False, True]
    assert "died" in scores[1].reasons["static"]


def test_a_worker_that_scores_nothing_is_an_error(tmp_path):
    broken = tmp_path / "worker.py"
    broken.write_text("raise SystemExit(2)\n", encoding="utf-8")
    with pytest.raises(v1_runner.V1Error, match="before scoring anything"):
        run_v1(
            ["a"],
            spec_data={},
            warehouse=tmp_path,
            seed=1,
            python=Path(sys.executable),
            work_dir=tmp_path / "work",
            worker=broken,
        )


@pytest.mark.network
def test_the_v1_environment_is_built_from_the_tag_and_matches_its_results(world, tmp_path):
    python = v1_runner.ensure_v1_python(cache=tmp_path / "cache", log=lambda _: None)
    assert str(tmp_path) in str(python)
    catalog, _ = world
    scores = run_v1(
        [CLEAN],
        spec_data=yaml.safe_load(SPEC.read_text(encoding="utf-8")),
        warehouse=warehouse_of(catalog),
        seed=42,
        python=python,
        work_dir=tmp_path / "work",
    )
    assert not scores[0].unreadable and json.dumps(scores[0].reasons)
