"""The second dbt search: pre-registration, screening and pins fit together."""

from __future__ import annotations

import csv
import json
import subprocess

import pytest
import yaml

from bench.realworld import checkout
from bench.realworld.checkout import ROOT, Project, load_projects
from sqlguard.leakage.spec import LeakageSpec

REALWORLD = ROOT / "bench" / "realworld"
ROUND2 = REALWORLD / "round2"
PROJECTS = load_projects(ROUND2 / "projects.yaml")
PREREG = yaml.safe_load((ROUND2 / "search-prereg.yaml").read_text(encoding="utf-8"))
STOP_QUALIFIED = 12
STOP_SCREENED = 150


def _screening() -> list[dict[str, str]]:
    with open(ROUND2 / "screening.csv", newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def test_projects_are_round2_pinned_to_full_commits_and_new_to_the_validation():
    assert {p.group for p in PROJECTS} == {"round2"}
    assert all(len(p.sha) == 40 for p in PROJECTS)
    assert len({p.repo for p in PROJECTS}) == len(PROJECTS)
    # a project without a license is never published as code
    assert all(p.publish == "aggregate_only" for p in PROJECTS if p.license == "none")
    assert all(p.publish == "code" for p in PROJECTS if p.license != "none")
    first_round = {p.repo for p in load_projects(REALWORLD / "projects.yaml")}
    assert not first_round & {p.repo for p in PROJECTS}


def test_load_projects_accepts_round2_and_rejects_an_unknown_group(tmp_path):
    entry = (
        "projects:\n  - {{repo: a/b, group: {group}, sha: '{sha}', license: MIT, publish: code}}\n"
    )
    good = tmp_path / "good.yaml"
    good.write_text(entry.format(group="round2", sha="0" * 40))
    assert load_projects(good)[0].group == "round2"
    bad = tmp_path / "bad.yaml"
    bad.write_text(entry.format(group="third", sha="0" * 40))
    with pytest.raises(ValueError, match="group must be one of"):
        load_projects(bad)


def test_round2_excludes_round1_screening_and_the_registered_exclusions():
    with open(REALWORLD / "screening.csv", newline="", encoding="utf-8") as handle:
        first_round = {r["repo"].lower() for r in csv.DictReader(handle)}
    registered = {e.lower() for e in PREREG["excluded"] if "/" in e and " " not in e}
    rows = _screening()
    for row in rows:
        repo = row["repo"].lower()
        if repo in first_round | registered:
            assert row["verdict"] == "excluded", row["repo"]
        else:
            assert row["verdict"] != "excluded", row["repo"]


def _applicable(repo: str) -> bool:
    path = ROUND2 / "specs" / f"{repo.replace('/', '__')}.yaml"
    block = yaml.safe_load(path.read_text(encoding="utf-8"))["realworld"]
    return block.get("applicable") is not False


def test_screening_follows_the_registered_stopping_rule_and_its_amendment():
    rows = _screening()
    repos = [r["repo"] for r in rows]
    assert len(repos) == len(set(repos))
    assert [int(r["order"]) for r in rows] == list(range(1, len(rows) + 1))
    for row in rows:
        if row["verdict"] == "rejected":
            assert row["failed_criterion"] in {"1", "2", "3", "4", "5"}, row["repo"]
            assert row["note"].strip()
        if row["verdict"] != "not_screened":
            assert row["pinned_sha"] == "" or len(row["pinned_sha"]) == 40
    screened = [r for r in rows if r["verdict"] in {"qualified", "rejected", "duplicate"}]
    qualified = [r for r in rows if r["verdict"] == "qualified"]
    runnable = [r for r in qualified if _applicable(r["repo"])]
    assert len(screened) <= STOP_SCREENED
    # first rule: 12 qualifiers by candidate 51; amendment: continue until 12 projects can run
    first_twelve = qualified[:STOP_QUALIFIED]
    assert len(first_twelve) == STOP_QUALIFIED and int(first_twelve[-1]["order"]) == 51
    assert len(runnable) == STOP_QUALIFIED or len(screened) == STOP_SCREENED
    # nothing is screened after the stop, which is the last runnable qualifier (or the 150th)
    stop = max(int(r["order"]) for r in screened)
    if len(runnable) == STOP_QUALIFIED:
        assert stop == int(runnable[-1]["order"])
    assert all(r["verdict"] in {"not_screened", "excluded"} for r in rows if int(r["order"]) > stop)
    assert all(r["verdict"] != "not_screened" for r in rows if int(r["order"]) <= stop)


def test_the_amendment_is_registered_before_the_continued_screening():
    amendment = PREREG["amendments"][0]
    assert "before the checker was run" in amendment["decided"]
    assert "#52" in amendment["change"] and "12 projects" in amendment["change"]
    assert "none is deleted" in amendment["unchanged"]


def test_the_projects_are_exactly_the_qualified_candidates_with_their_pins():
    qualified = {r["repo"]: r for r in _screening() if r["verdict"] == "qualified"}
    assert [p.repo for p in PROJECTS] == list(qualified)
    entries = yaml.safe_load((ROUND2 / "projects.yaml").read_text(encoding="utf-8"))["projects"]
    for entry in entries:
        row = qualified[entry["repo"]]
        assert entry["sha"] == row["pinned_sha"]
        assert entry["default_branch"] == row["default_branch"]
        assert str(entry["license"]).lower() == row["license"]
        assert entry["screening_order"] == int(row["order"])
        # the cutoff flag is written once in the screening note and once in the pin
        assert ("no_documented_cutoff" in row["note"]) == (
            "no_documented_cutoff" in entry.get("flags", [])
        )


def test_search_results_are_those_the_screening_started_from():
    data = json.loads((ROUND2 / "search-results.json").read_text(encoding="utf-8"))
    assert [s["id"] for s in data["searches"]] == [s["id"] for s in PREREG["searches"]]
    assert not [s["id"] for s in data["searches"] if s["failed"]]
    order: list[str] = []
    for search in data["searches"]:
        for hit in search["results"]:
            if search["kind"] == "repos":
                name = hit["fullName"]
            else:
                name = hit["repository"]["nameWithOwner"]
            if name not in order:
                order.append(name)
    assert [r["repo"] for r in _screening()] == order


def test_compile_runs_dbt_deps_when_only_the_lock_file_is_committed(tmp_path, monkeypatch):
    monkeypatch.setattr(checkout, "WORK", tmp_path)
    project = Project(repo="o/r", group="round2", sha="0" * 40, license="MIT", publish="code")
    project.dbt_dir.mkdir(parents=True)
    (project.dbt_dir / "package-lock.yml").write_text("packages: []\n")
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


def _spec(project):
    path = ROUND2 / "specs" / f"{project.spec_name}.yaml"
    return path, yaml.safe_load(path.read_text(encoding="utf-8"))


def test_every_project_has_a_spec_that_is_valid_or_says_why_not_applicable():
    assert {p.spec_name for p in PROJECTS} == {
        path.stem for path in (ROUND2 / "specs").glob("*.yaml")
    }
    for project in PROJECTS:
        _, spec = _spec(project)
        block = spec["realworld"]
        if block.get("applicable") is False:
            assert block["reason"].strip(), project.repo
            continue
        parsed = LeakageSpec.from_dict(spec)  # the checker's own validation
        assert parsed.boundary == "strict"  # nothing relaxed without documentation
        assert not parsed.availability_columns and not parsed.max_ingest_delay
        assert block["training_sets"] and block["basis"].strip(), project.repo
        assert block["schema"], project.repo


def test_each_spec_states_its_cutoff_documentation_like_the_pin_does():
    entries = yaml.safe_load((ROUND2 / "projects.yaml").read_text(encoding="utf-8"))["projects"]
    flags = {e["repo"]: e.get("flags", []) for e in entries}
    for project in PROJECTS:
        path, _ = _spec(project)
        text = path.read_text(encoding="utf-8")
        flagged = "no_documented_cutoff" in flags[project.repo]
        assert ("Cutoff documentation: none (flag no_documented_cutoff)" in text) == flagged
        assert ("Cutoff documentation: documented" in text) == (not flagged), project.repo
