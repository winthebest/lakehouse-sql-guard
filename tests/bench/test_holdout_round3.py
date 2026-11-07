"""The registered protocol of the third dbt search: who is excluded, how a blind reader's line is
matched to a finding, how a finding is labelled, and the checks on a spec that declares roles."""

import csv
import json
from pathlib import Path

import pytest
import yaml

from bench.realworld.round3 import adjudication, blind_read, exclusions, spec_gate

ROOT = Path(".")
PREREG = ROOT / "bench/realworld/round3/search-prereg.yaml"


def read_csv(path: str) -> list[dict]:
    with open(ROOT / path, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


# --- exclusions -----------------------------------------------------------------------------


def test_every_repository_screened_in_the_earlier_rounds_is_excluded():
    excluded = exclusions.load_exclusions(ROOT)
    round1 = {r["repo"] for r in read_csv("bench/realworld/screening.csv")}
    round2 = {r["repo"] for r in read_csv("bench/realworld/round2/screening.csv")}
    assert round1 and round2
    assert {r.lower() for r in round1 | round2} <= set(excluded)


def test_every_hit_of_the_second_search_is_excluded_screened_or_not():
    excluded = exclusions.load_exclusions(ROOT)
    hits = json.loads((ROOT / "bench/realworld/round2/search-results.json").read_text())
    names = set()
    for search in hits["searches"]:
        for result in search["results"]:
            names.add((result.get("fullName") or result["repository"]["nameWithOwner"]).lower())
    assert len(names) > 100
    assert names <= set(excluded)
    # candidates the second search listed but never reached are excluded like the screened ones
    unreached = {
        r["repo"].lower()
        for r in read_csv("bench/realworld/round2/screening.csv")
        if r["verdict"] == "not_screened"
    }
    assert len(unreached) > 50 and unreached <= set(excluded)
    assert all("round2 search hit" in excluded[name] for name in unreached)


def test_the_repositories_named_as_read_during_development_are_excluded():
    excluded = exclusions.load_exclusions(ROOT)
    assert "dbt-labs/jaffle_shop_duckdb" in excluded
    assert "fal-ai/dbt_feature_store" in excluded


def test_a_candidate_is_compared_without_regard_to_case():
    found = exclusions.excluded_among(["DBT-LABS/jaffle_shop_duckdb", "someone/brand-new"], ROOT)
    assert found == ["DBT-LABS/jaffle_shop_duckdb"]


def test_forks_and_renames_are_not_guessed_at():
    # only exact repository names are compared: judging "is a copy of" is the screener's job
    assert exclusions.excluded_among(["someone/jaffle_shop_duckdb"], ROOT) == []


def test_the_registered_exclusion_sources_have_the_digests_of_the_files():
    prereg = yaml.safe_load(PREREG.read_text(encoding="utf-8"))
    recorded = prereg["exclusion"]["sources"]
    assert {s["path"] for s in recorded} == {p for _, p in exclusions.SOURCES}
    assert {s["path"]: s["sha256"] for s in recorded} == exclusions.digests(ROOT)
    assert prereg["exclusion"]["count"] == len(exclusions.load_exclusions(ROOT))


# --- the registered searches ------------------------------------------------------------------


def commands(path: Path) -> list[str]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return [e["command"] if isinstance(e, dict) else e for e in data["searches"]]


def test_no_search_repeats_one_of_the_earlier_rounds():
    new = commands(PREREG)
    old = commands(ROOT / "bench/realworld/search-prereg.yaml") + commands(
        ROOT / "bench/realworld/round2/search-prereg.yaml"
    )
    assert len(new) >= 12 and len(set(new)) == len(new)
    assert not {c.lower() for c in new} & {c.lower() for c in old}
    ids = [e["id"] for e in yaml.safe_load(PREREG.read_text(encoding="utf-8"))["searches"]]
    assert len(set(ids)) == len(ids)


def test_the_stop_rule_the_blind_read_and_the_three_labels_are_registered():
    prereg = yaml.safe_load(PREREG.read_text(encoding="utf-8"))
    stop = prereg["selection"]["stop"]
    assert "12" in stop and "200" in stop
    assert prereg["rules"] == "rules-frozen-v4"
    assert set(prereg["adjudication"]["labels"]) == {
        "true",
        "true_not_actionable",
        "false",
        "unsure",
    }
    for label, entry in prereg["adjudication"]["labels"].items():
        assert entry["definition"] and entry["example"], label
    assert prereg["blind_read"]["columns"] == list(blind_read.READER_COLUMNS)
    assert prereg["blind_read"]["groups"] == {
        group: sorted(t for t, g in blind_read.LEAK_GROUPS.items() if g == group)
        for group in sorted(set(blind_read.LEAK_GROUPS.values()))
    }
    assert prereg["specs"]["per_project_files"] == [
        "specs/<owner>__<repo>.yaml",
        "specs-noroles/<owner>__<repo>.yaml",
    ]


# --- the blind reader -------------------------------------------------------------------------


def reader(**kw):
    row = {
        "id": "br-01",
        "project": "o/r",
        "model": "fct_features",
        "feature": "avg_discount",
        "source": "stg_promotions",
        "leak_type": "L3",
        "evidence": "models/fct_features.sql:14",
        "note": "",
    }
    return {**row, **kw}


def finding(**kw):
    row = {
        "id": "o/r#fct_features#1",
        "project": "o/r",
        "training_model": "fct_features",
        "rule": "L3",
        "location": "stg_promotions",
        "message": "avg_discount aggregates stg_promotions without an upper bound",
        "evidence": '{"table": "stg_promotions"}',
    }
    return {**row, **kw}


def test_a_reader_line_matches_a_finding_of_the_same_model_source_and_group():
    assert blind_read.matches(reader(), finding())


def test_the_same_source_table_is_enough_when_the_column_differs():
    assert blind_read.matches(reader(feature="other_col"), finding(message="no column named"))


def test_the_same_feature_column_is_enough_when_the_table_differs():
    assert blind_read.matches(reader(source="raw.promos"), finding(location="int_promotions"))


def test_a_qualified_source_name_is_reduced_to_its_last_part():
    assert blind_read.matches(reader(source="analytics.staging.STG_Promotions"), finding())


@pytest.mark.parametrize(
    "change",
    [
        {"project": "o/other"},
        {"model": "fct_other"},
        {"leak_type": "L6"},  # another group
        {"leak_type": "L7"},
    ],
)
def test_other_project_model_or_group_do_not_match(change):
    assert not blind_read.matches(reader(**change), finding())


def test_neither_the_column_nor_the_source_in_common_does_not_match():
    assert not blind_read.matches(
        reader(feature="x", source="stg_orders"),
        finding(location="stg_promotions", message="avg_discount over stg_promotions"),
    )


def test_a_column_name_inside_a_longer_word_is_not_the_column():
    assert not blind_read.matches(
        reader(feature="discount", source="stg_orders"),
        finding(location="stg_promotions", message="avg_discount aggregates stg_promotions"),
    )


def test_leak_types_are_grouped_as_registered():
    groups = blind_read.LEAK_GROUPS
    assert groups["L1"] == groups["L2"] == groups["L3"] == groups["L5"] != groups["L4"]
    assert len({groups["L4"], groups["L6"], groups["L7"], groups["L1"]}) == 4
    with pytest.raises(ValueError):
        blind_read.group_of("L9")


def test_misses_and_recall_by_the_reader_count_only_lines_judged_true():
    readers = [
        reader(id="br-01"),
        reader(id="br-02", model="m2", source="s2", feature="f2"),
        reader(id="br-03", model="m3", source="s3", feature="f3"),
        reader(id="br-04", model="m4", source="s4", feature="f4"),
    ]
    labels = {"br-01": "true", "br-02": "true", "br-03": "false", "br-04": "true_not_actionable"}
    result = blind_read.reader_recall(readers, [finding()], labels)
    assert result["true_lines"] == 2 and result["matched"] == 1
    assert result["missed"] == ["br-02"]
    assert result["recall_by_reader"] == 0.5
    assert result["not_actionable_lines"] == 1
    assert result["lower95"] is not None and result["lower95"] < 0.5


def test_recall_by_the_reader_is_undefined_without_a_true_line():
    result = blind_read.reader_recall([reader()], [finding()], {"br-01": "false"})
    assert result["true_lines"] == 0
    assert result["recall_by_reader"] is None and result["lower95"] is None


def test_a_line_without_an_adjudicated_label_is_an_error():
    with pytest.raises(ValueError, match="br-01"):
        blind_read.reader_recall([reader()], [finding()], {})


def test_a_reader_file_is_validated(tmp_path):
    path = tmp_path / "blind-read.csv"
    header = ",".join(blind_read.READER_COLUMNS)
    path.write_text(header + "\nbr-01,o/r,m,f,s,L3,e.sql:1,\n", encoding="utf-8")
    assert blind_read.read_reader_file(path)[0]["leak_type"] == "L3"
    path.write_text(header + "\nbr-01,o/r,m,f,s,L9,e.sql:1,\n", encoding="utf-8")
    with pytest.raises(ValueError, match="L9"):
        blind_read.read_reader_file(path)
    path.write_text(header + "\nbr-01,o/r,m,f,s,L3,e.sql:1,\nbr-01,o/r,m,f,s,L3,e.sql:2,\n")
    with pytest.raises(ValueError, match="twice"):
        blind_read.read_reader_file(path)
    path.write_text("id,project\nbr-01,o/r\n")
    with pytest.raises(ValueError, match="columns"):
        blind_read.read_reader_file(path)


# --- three labels -----------------------------------------------------------------------------


def test_two_reviewers_who_agree_give_the_final_label_and_otherwise_it_is_unsure():
    assert adjudication.final_label("true", "true") == "true"
    both = "true_not_actionable"
    assert adjudication.final_label(both, both) == both
    assert adjudication.final_label("true", "false") == "unsure"
    assert adjudication.final_label("true", "true_not_actionable") == "unsure"
    assert adjudication.final_label("unsure", "unsure") == "unsure"
    with pytest.raises(ValueError):
        adjudication.final_label("maybe", "true")


def test_the_registered_precision_and_effective_false_positive_rate():
    finals = ["true"] * 5 + ["true_not_actionable"] * 3 + ["false"] * 8 + ["unsure"] * 4
    m = adjudication.finding_metrics(finals)
    assert m["findings"] == 20
    assert m["precision_strict"] == 0.25  # true / findings, unsure counted against
    assert m["precision_loose"] == 0.4  # (true + true_not_actionable) / findings
    assert m["effective_fp"] == 0.75  # (false + true_not_actionable + unsure) / findings
    assert m["counts"] == {"true": 5, "true_not_actionable": 3, "false": 8, "unsure": 4}
    assert 0 < m["precision_strict_lower95"] < 0.25


def test_the_h2_goal_is_met_on_the_lower_bound():
    assert adjudication.finding_metrics(["true"] * 14 + ["false"] * 16)["meets_goal"] is True
    assert adjudication.finding_metrics(["true"] * 13 + ["false"] * 17)["meets_goal"] is False
    assert adjudication.finding_metrics([])["meets_goal"] is False
    assert adjudication.finding_metrics([])["precision_strict"] is None


# --- a spec that declares roles ---------------------------------------------------------------

GOOD_SPEC = """\
label_table: spine
entity_keys: [user_id]
prediction_ts: cutoff_ts
label_columns: [churned]
time_columns: {events: event_ts}
output_roles:
  label:
    - next_day_return   # docs/model.md:42 "the next-day return is what is predicted"
  metadata:
    # README.md:17 (the loaded_at column is added by the incremental macro)
    - loaded_at
"""
CITED = '# docs/model.md:42 "the next-day return is what is predicted"'


def test_a_role_with_a_citation_passes_the_gate(tmp_path):
    path = tmp_path / "spec.yaml"
    path.write_text(GOOD_SPEC)
    assert spec_gate.check_role_citations(path) == []


def test_a_role_without_a_citation_fails_the_gate(tmp_path):
    path = tmp_path / "spec.yaml"
    path.write_text(GOOD_SPEC.replace(CITED, ""))
    problems = spec_gate.check_role_citations(path)
    assert len(problems) == 1 and "next_day_return" in problems[0]


def test_a_flow_list_of_roles_fails_the_gate(tmp_path):
    path = tmp_path / "spec.yaml"
    flow = GOOD_SPEC.split("  metadata:")[0] + "  metadata: [loaded_at]\n"
    path.write_text(flow)
    assert any("one column per line" in p for p in spec_gate.check_role_citations(path))


@pytest.mark.parametrize(
    "comment", ["# see the docs", "# docs/model.md", "# line 42", "# 42", "# docs/model.md:abc"]
)
def test_a_citation_needs_a_file_and_a_line_number(tmp_path, comment):
    path = tmp_path / "spec.yaml"
    path.write_text(GOOD_SPEC.replace(CITED, comment))
    assert spec_gate.check_role_citations(path), comment


def test_a_spec_without_roles_has_nothing_to_cite(tmp_path):
    path = tmp_path / "spec.yaml"
    path.write_text(GOOD_SPEC.split("output_roles:")[0])
    assert spec_gate.check_role_citations(path) == []


def test_the_spec_without_roles_is_the_same_text_minus_the_roles_block():
    text = GOOD_SPEC + "dialect: duckdb\n"
    stripped = spec_gate.strip_output_roles(text)
    assert "output_roles" not in stripped and "next_day_return" not in stripped
    assert stripped.endswith("dialect: duckdb\n")
    kept = {k: v for k, v in yaml.safe_load(text).items() if k != "output_roles"}
    assert yaml.safe_load(stripped) == kept
    assert spec_gate.strip_output_roles("a: 1\n") == "a: 1\n"
