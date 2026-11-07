"""The pure parts of the real-world runner: annotate, catalog, rows, CSV, frozen-source check."""

from __future__ import annotations

import csv
import subprocess

import pytest

from bench.realworld import rows

MANIFEST = {
    "nodes": {
        "model.p.stg_users": {
            "resource_type": "model",
            "original_file_path": "models/stg_users.sql",
            "compiled_code": "select user_id, churn_date from raw.users",
            "depends_on": {"nodes": ["source.p.raw.users"]},
        },
        "model.p.int_events": {
            "resource_type": "model",
            "original_file_path": "models/int_events.sql",
            "compiled_code": "\n".join(
                [f"-- line {i}" for i in range(60)]
                + ["select user_id, count(*) over (partition by user_id) as n from raw.events"]
            ),
            "depends_on": {"nodes": ["source.p.raw.events", "model.p.stg_users"]},
        },
        "model.p.train": {
            "resource_type": "model",
            "original_file_path": "models/train.sql",
            "compiled_code": "select * from int_events",
            "depends_on": {"nodes": ["model.p.int_events", "model.p.stg_users"]},
        },
        "model.p.other": {
            "resource_type": "model",
            "original_file_path": "models/other.sql",
            "compiled_code": "select 1",
            "depends_on": {"nodes": []},
        },
        "seed.p.raw_orders": {"resource_type": "seed", "depends_on": {"nodes": []}},
    },
    "sources": {
        "source.p.raw.users": {"resource_type": "source"},
        "source.p.raw.events": {"resource_type": "source"},
    },
}


def test_resolve_node_by_kind_and_name_without_package():
    assert rows.resolve_node(MANIFEST, "model:train") == "model.p.train"
    assert rows.resolve_node(MANIFEST, "source:raw.users") == "source.p.raw.users"
    assert rows.resolve_node(MANIFEST, "seed:raw_orders") == "seed.p.raw_orders"


@pytest.mark.parametrize("ref", ["model:missing", "source:users", "train", "model:"])
def test_resolve_node_rejects_unknown_or_malformed_references(ref):
    with pytest.raises(rows.RealworldError):
        rows.resolve_node(MANIFEST, ref)


def test_annotate_sets_meta_on_a_copy_and_leaves_the_original_alone():
    block = {
        "training_sets": ["train"],
        "meta": {
            "model:stg_users": {"role": "label_spine"},
            "source:raw.events": {"time_column": "t"},
        },
    }
    annotated = rows.annotate_manifest(MANIFEST, block)
    assert annotated["nodes"]["model.p.train"]["config"]["meta"]["leakage"] == {
        "role": "training_set"
    }
    assert annotated["nodes"]["model.p.stg_users"]["config"]["meta"]["leakage"]["role"] == (
        "label_spine"
    )
    assert annotated["sources"]["source.p.raw.events"]["config"]["meta"]["leakage"] == {
        "time_column": "t"
    }
    assert "config" not in MANIFEST["nodes"]["model.p.train"]


def test_annotate_reports_a_training_set_that_is_not_a_model():
    with pytest.raises(rows.RealworldError):
        rows.annotate_manifest(MANIFEST, {"training_sets": ["raw_orders"]})


def test_catalog_has_the_layout_of_dbt_docs_generate():
    catalog = rows.catalog_from_schema(
        MANIFEST,
        {"source:raw.users": {"user_id": "INTEGER"}, "model:stg_users": {"churn_date": "DATE"}},
    )
    assert catalog["sources"]["source.p.raw.users"]["columns"]["user_id"] == {
        "name": "user_id",
        "type": "INTEGER",
    }
    assert catalog["nodes"]["model.p.stg_users"]["columns"]["churn_date"]["type"] == "DATE"


def test_upstream_models_follow_models_only_and_stop_at_physical_tables():
    assert rows.upstream_models(MANIFEST, "model.p.train") == [
        "model.p.train",
        "model.p.int_events",
        "model.p.stg_users",
    ]
    assert rows.upstream_models(MANIFEST, "model.p.train", stop_at=["model.p.stg_users"]) == [
        "model.p.train",
        "model.p.int_events",
    ]


def test_boundary_models_are_those_declaring_a_role_or_time_column():
    annotated = rows.annotate_manifest(
        MANIFEST,
        {
            "meta": {
                "model:stg_users": {"role": "label_spine"},
                "model:other": {"time_column": "ts"},
                "model:int_events": {"role": "training_set"},
            }
        },
    )
    assert sorted(rows.boundary_models(annotated)) == ["model.p.other", "model.p.stg_users"]


def test_excerpt_is_centered_on_the_match_and_cut_on_line_boundaries():
    code = MANIFEST["nodes"]["model.p.int_events"]["compiled_code"]
    text = rows.excerpt(code, rows.needles({"location": "events AS e"}), max_lines=10)
    lines = text.splitlines()
    assert len(lines) == 10
    assert any("raw.events" in line for line in lines)
    assert rows.excerpt("select 1", ["x"]) == "select 1"


def test_needles_prefer_the_window_frame_over_the_location():
    finding = {
        "location": "bronze AS b",
        "evidence": {
            "window": (
                "COUNT(x) OVER (PARTITION BY u ORDER BY t "
                "RANGE BETWEEN 3600 PRECEDING AND 1 PRECEDING)"
            )
        },
    }
    assert (
        rows.needles(finding)[0]
        == "count(x)over(partitionbyuorderbytrangebetween3600precedingand1preceding)"
    )
    assert "rangebetween3600precedingand1preceding" in rows.needles(finding)
    assert "bronze" in rows.needles(finding)


def _entry(findings, node="model.p.train"):
    return {
        "node": node,
        "file": "models/train.sql",
        "verdict": "leaky",
        "score": 0.97,
        "findings": findings,
    }


def test_finding_rows_split_leaks_from_unchecked_and_locate_the_flagged_scope():
    entries = [
        _entry(
            [
                {
                    "rule_id": "L3",
                    "severity": "high",
                    "message": "window spans the whole partition",
                    "location": "events AS e",
                    "evidence": {"window": "COUNT(*) OVER (PARTITION BY user_id)"},
                    "models": [{"node": "model.p.train", "file": "models/train.sql"}],
                    "attribution": "candidates",
                },
                {"rule_id": "SPEC", "severity": "low", "message": "no prediction time"},
            ]
        )
    ]
    leaks, unchecked = rows.finding_rows({"repo": "o/p", "group": "new"}, entries, MANIFEST)
    assert [r["rule"] for r in leaks] == ["L3"]
    assert [r["rule"] for r in unchecked] == ["SPEC"]
    row = leaks[0]
    assert row["id"] == "o/p#train#1"
    assert row["training_model"] == "train"
    assert row["model_verdict"] == "leaky"
    # the candidate model has no matching line, so the model that holds the window is found
    assert row["compiled_sql_excerpt"].startswith("-- models/int_events.sql")
    assert "over (partition by user_id)" in row["compiled_sql_excerpt"]
    assert all(row[c] == "" for c in rows.ADJUDICATION_COLUMNS)


def test_public_row_withholds_code_of_unlicensed_projects_only():
    row = {"evidence": '{"a":1}', "compiled_sql_excerpt": "select 1", "rule": "L1"}
    assert rows.public_row(row, "code") == row
    hidden = rows.public_row(row, "aggregate_only")
    assert hidden["compiled_sql_excerpt"] == rows.WITHHELD
    assert hidden["evidence"] == rows.WITHHELD
    assert hidden["rule"] == "L1"
    assert rows.public_row({**row, "evidence": ""}, "aggregate_only")["evidence"] == ""


def test_write_csv_round_trips_commas_quotes_and_newlines(tmp_path):
    row = {
        "id": "a#m#1",
        "message": 'has, comma and "quotes"',
        "compiled_sql_excerpt": "select 1\nfrom t",
    }
    path = tmp_path / "out" / "findings.csv"
    assert rows.write_csv(path, [row]) == 1
    with open(path, newline="", encoding="utf-8") as handle:
        read = list(csv.DictReader(handle))
    assert list(read[0]) == list(rows.FINDING_COLUMNS)
    assert read[0]["message"] == row["message"]
    assert read[0]["compiled_sql_excerpt"] == row["compiled_sql_excerpt"]
    assert read[0]["final"] == ""


def test_write_csv_rejects_a_column_it_was_not_given():
    with pytest.raises(ValueError):
        rows.write_csv("/dev/null", [{"unknown": 1}], ["id"])


def test_model_rows_list_every_checked_model_clean_ones_included():
    records = [
        {
            "repo": "o/p",
            "group": "seen",
            "status": "checked",
            "models": [
                {"model": "m", "verdict": "clean", "findings": 0, "unchecked": 0, "seconds": 0.1}
            ],
        },
        {"repo": "o/q", "group": "new", "status": "not_applicable"},
    ]
    found = rows.model_rows(records)
    assert [(r["project"], r["verdict"]) for r in found] == [("o/p", "clean")]
    assert found[0]["final"] == ""


LOCK = """version = 1

[[package]]
name = "lakehouse-sql-guard"
version = "{version}"

[[package]]
name = "sqlglot"
version = "{sqlglot}"
"""


class _Git:
    """A fake `subprocess.run` that answers the three git commands of the frozen check."""

    def __init__(self, diff_code=0, status_out="", tag_lock=None):
        self.diff_code, self.status_out, self.tag_lock, self.calls = (
            diff_code,
            status_out,
            tag_lock or LOCK.format(version="0.1.1", sqlglot="27.0"),
            [],
        )

    def __call__(self, command, **_):
        self.calls.append(command)
        if command[1] == "diff":
            return subprocess.CompletedProcess(command, self.diff_code, "", "bad tag")
        if command[1] == "show":
            return subprocess.CompletedProcess(command, 0, self.tag_lock, "")
        return subprocess.CompletedProcess(command, 0, self.status_out, "")


def _repo(tmp_path, version="0.2.0", sqlglot="27.0"):
    (tmp_path / "uv.lock").write_text(LOCK.format(version=version, sqlglot=sqlglot))
    return tmp_path


def test_frozen_check_passes_when_the_leakage_code_matches_the_tag(tmp_path):
    git = _Git()
    rows.assert_rules_frozen(_repo(tmp_path), run=git)
    assert git.calls[0] == [
        "git",
        "diff",
        "--quiet",
        "rules-frozen-v3",
        "HEAD",
        "--",
        *rows.FROZEN_PATHS,
    ]
    assert "src/sqlguard/leakage" in rows.FROZEN_PATHS and "src/sqlguard/core" in rows.FROZEN_PATHS
    assert not any(p.startswith("src/sqlguard/approvals") for p in rows.FROZEN_PATHS)


def test_frozen_check_fails_when_the_leakage_code_differs_from_the_tag(tmp_path):
    with pytest.raises(rows.RealworldError, match="not frozen"):
        rows.assert_rules_frozen(_repo(tmp_path), run=_Git(diff_code=1))


def test_frozen_check_fails_when_git_cannot_compare(tmp_path):
    with pytest.raises(rows.RealworldError, match="cannot compare"):
        rows.assert_rules_frozen(_repo(tmp_path), run=_Git(diff_code=128))


def test_frozen_check_fails_on_uncommitted_source_changes(tmp_path):
    with pytest.raises(rows.RealworldError, match="uncommitted"):
        rows.assert_rules_frozen(
            _repo(tmp_path), run=_Git(status_out=" M src/sqlguard/leakage/x.py\n")
        )


def test_frozen_check_fails_when_a_locked_dependency_changes(tmp_path):
    with pytest.raises(rows.RealworldError, match="locked dependencies"):
        rows.assert_rules_frozen(_repo(tmp_path, sqlglot="28.0"), run=_Git())


def test_frozen_check_matches_this_checkout():
    """The guard passes on a checkout whose rules equal the frozen tag and refuses one that differs.

    Between freezes the rules are under development, so this checkout may differ from the tag; the
    guard must then refuse, so no result can be recorded as produced by the frozen rules.
    """
    from bench.realworld.checkout import ROOT

    tag = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", rows.FROZEN_TAG],
        cwd=ROOT,
        capture_output=True,
    )
    if tag.returncode != 0:
        pytest.skip(f"tag {rows.FROZEN_TAG} is not in this clone")
    changed = subprocess.run(
        ["git", "diff", "--quiet", rows.FROZEN_TAG, "--", *rows.FROZEN_PATHS],
        cwd=ROOT,
        capture_output=True,
    )
    if changed.returncode == 0:
        rows.assert_rules_frozen(ROOT)
    else:
        with pytest.raises(rows.RealworldError):
            rows.assert_rules_frozen(ROOT)


def test_a_rerun_keeps_the_hand_verdicts_and_the_projects_it_did_not_run(tmp_path):
    existing = tmp_path / "findings.csv"
    old = [
        {"id": "a#m#1", "project": "a", "controller": "true", "reviewer": "true", "final": "true"},
        {
            "id": "b#m#1",
            "project": "b",
            "controller": "false",
            "reviewer": "false",
            "final": "false",
        },
    ]
    rows.write_csv(existing, old)
    rerun = [{"id": "a#m#1", "project": "a"}, {"id": "a#m#2", "project": "a"}]
    merged = rows.keep_adjudication(rerun, existing, {"a"})
    by_id = {r["id"]: r for r in merged}
    assert by_id["a#m#1"]["final"] == "true" and "final" not in by_id["a#m#2"]
    assert by_id["b#m#1"]["final"] == "false" and len(merged) == 3
