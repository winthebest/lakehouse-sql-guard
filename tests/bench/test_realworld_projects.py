"""The committed project list, specs, screening and mutation operators fit together."""

from __future__ import annotations

import csv
import re
from collections import Counter

import pytest
import yaml

from bench.realworld.checkout import ROOT, Project, load_projects
from bench.realworld.mutate import (
    Mutant,
    apply_operator,
    load_operators,
    mutants,
    recall,
    score_mutant,
)
from sqlguard.leakage.spec import LeakageSpec

REALWORLD = ROOT / "bench" / "realworld"
PROJECTS = load_projects(REALWORLD / "projects.yaml")


def test_projects_are_pinned_to_full_commits_and_split_in_two_groups():
    assert {p.group for p in PROJECTS} == {"new", "seen"}
    assert all(len(p.sha) == 40 for p in PROJECTS)
    assert len({p.repo for p in PROJECTS}) == len(PROJECTS)
    # a project without a license is never published as code
    assert all(p.publish == "aggregate_only" for p in PROJECTS if p.license == "none")


def test_load_projects_rejects_a_short_sha(tmp_path):
    bad = tmp_path / "p.yaml"
    bad.write_text(
        "projects:\n  - {repo: a/b, group: new, sha: abc123, license: MIT, publish: code}\n"
    )
    with pytest.raises(ValueError, match="40-character"):
        load_projects(bad)


def test_every_project_has_a_spec_and_a_variant_uses_its_projects_spec():
    for project in PROJECTS:
        assert (REALWORLD / "specs" / f"{project.slug}.yaml").is_file()
    variant = Project("a/b", "new", "0" * 40, "MIT", "code", variant="m1")
    assert variant.spec_name == "a__b"
    assert variant.slug == "a__b__m1"


@pytest.mark.parametrize("project", PROJECTS, ids=lambda p: p.repo)
def test_specs_are_valid_leakage_specs_or_say_why_not_applicable(project):
    spec = yaml.safe_load((REALWORLD / "specs" / f"{project.slug}.yaml").read_text())
    block = spec["realworld"]
    if block.get("applicable") is False:
        assert block["reason"].strip()
        return
    parsed = LeakageSpec.from_dict(spec)  # the checker's own validation
    assert parsed.boundary == "strict"  # nothing relaxed without documentation
    assert block["training_sets"] and block["basis"].strip()
    assert block["schema"]


def test_screening_lists_every_candidate_once_with_a_failed_criterion_for_rejections():
    with open(REALWORLD / "screening.csv", newline="", encoding="utf-8") as handle:
        screened = list(csv.DictReader(handle))
    repos = [r["repo"] for r in screened]
    assert len(repos) == len(set(repos))
    for row in screened:
        if row["verdict"] == "rejected":
            assert row["failed_criterion"] in {"1", "2", "3", "4", "5"}
    qualified = [r["repo"] for r in screened if r["verdict"] == "qualified"]
    new = [p.repo for p in PROJECTS if p.group == "new"]
    assert qualified == new  # the new group is exactly the projects that qualified


def test_search_results_are_those_the_screening_started_from():
    import json

    data = json.loads((REALWORLD / "search-results.json").read_text())
    order = []
    for search in data["searches"]:
        order += [r["fullName"] for r in search["results"] if r["fullName"] not in order]
    with open(REALWORLD / "screening.csv", newline="", encoding="utf-8") as handle:
        assert [r["repo"] for r in csv.DictReader(handle)] == order


def test_operators_cover_the_leak_types_except_l4_and_use_valid_regexes():
    data = load_operators()
    leaks = {op["leak"] for op in data["operators"]}
    assert leaks == {"L1", "L2", "L3", "L5", "L6", "L7"}
    for op in data["operators"]:
        assert op["why_leaky"].strip() and op["models"] and op["edits"]
        for edit in op["edits"]:
            re.compile(edit["find"])
    assert len(mutants(data)) == len({m.id for m in mutants(data)})


def test_apply_operator_edits_in_order_and_fails_when_nothing_matches():
    operator = {
        "id": "x",
        "edits": [
            {
                "find": r"between (\d+) preceding",
                "replace": r"between 1 following and \1 following",
            },
            {"find": "from t\n", "replace": "from t\nleft join u using (id)\n"},
        ],
    }
    text = "select sum(a) over (range between 30 preceding and 1 preceding)\nfrom t\n"
    mutated = apply_operator(text, operator)
    assert "between 1 following and 30 following and 1 preceding" in mutated
    assert "left join u using (id)" in mutated
    with pytest.raises(ValueError, match="matches nothing"):
        apply_operator("select 1", operator)


def _row(rule, evidence="", file="models/a.sql", location="t"):
    return {"rule": rule, "model_file": file, "location": location, "evidence": evidence}


def test_score_counts_a_finding_the_control_already_has_as_masked():
    control = [_row("L1", "w1")]
    masked = score_mutant(control, [_row("L1", "w1")], "leaky", "L1", "models/a.sql")
    assert masked["caught_same_rule"] and masked["masked"] and not masked["caught_new"]
    assert not masked["exploratory_evidence_changed"]
    # a changed window under the same rule and place is not a new finding by the registered rule
    changed = score_mutant(control, [_row("L1", "w2")], "leaky", "L1", "models/a.sql")
    assert changed["masked"] and changed["exploratory_evidence_changed"]


def test_score_catches_a_new_finding_and_checks_the_model_that_holds_it():
    control = [_row("L1", "w1")]
    caught = score_mutant(
        control, [*control, _row("L2", file="models/b.sql")], "leaky", "L2", "models/b.sql"
    )
    assert caught["caught_new"] and caught["right_model"] and not caught["masked"]
    wrong = score_mutant(control, [_row("L2", file="models/c.sql")], "leaky", "L2", "models/b.sql")
    assert wrong["caught_same_rule"] and not wrong["right_model"]
    missed = score_mutant(control, [], "clean", "L7", "models/b.sql")
    assert not missed["caught_same_rule"] and not missed["leaky"]


def test_recall_is_reported_per_operator_and_per_leak_type():
    results = [
        {
            "operator": "o1",
            "leak": "L1",
            "caught_same_rule": True,
            "caught_new": False,
            "leaky": True,
            "right_model": True,
        },
        {
            "operator": "o1",
            "leak": "L1",
            "caught_same_rule": True,
            "caught_new": True,
            "leaky": True,
            "right_model": False,
        },
        {
            "operator": "o2",
            "leak": "L2",
            "caught_same_rule": False,
            "caught_new": False,
            "leaky": False,
            "right_model": False,
        },
    ]
    by_leak = recall(results, "leak")
    assert by_leak["L1"]["mutants"] == 2
    assert by_leak["L1"]["recall_same_rule"] == 1.0
    assert by_leak["L1"]["recall_new"] == 0.5
    assert by_leak["L2"]["recall_same_rule"] == 0.0
    assert Counter(r["operator"] for r in results) == Counter({"o1": 2, "o2": 1})
    assert Mutant("o1", "L1", "m").id == "o1__m"


def test_compile_runs_dbt_deps_when_the_project_has_packages(tmp_path, monkeypatch):
    import subprocess

    from bench.realworld import checkout

    monkeypatch.setattr(checkout, "WORK", tmp_path)
    project = Project(repo="o/r", group="seen", sha="0" * 40, license="mit", publish="full")
    project.dbt_dir.mkdir(parents=True)
    (project.dbt_dir / "packages.yml").write_text("packages: []\n")
    (project.dbt_dir / "dbt_project.yml").write_text("name: r\nprofile: r\n")
    calls = []

    def fake_dbt(args, proj, profiles):
        calls.append(args[0])
        if args[0] == "compile":
            proj.manifest.parent.mkdir(parents=True, exist_ok=True)
            proj.manifest.write_text("{}")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(checkout, "_dbt", fake_dbt)
    assert checkout.compile_project(project)["deps"] is True and calls == ["deps", "compile"]
    calls.clear()
    assert checkout.compile_project(project, deps=False)["deps"] is False and calls == ["compile"]
