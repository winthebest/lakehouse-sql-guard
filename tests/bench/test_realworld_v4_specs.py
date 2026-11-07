"""The v4 specs of the second dbt round: the old spec plus fields that cite the project's files.

A role, a label delay or a validity column that a spec declares can silence a finding, so each one
must say which line of the project's own files states it. The comment right above the field holds
the citation (`path:line`); a field without one is not allowed.
"""

import re
from pathlib import Path

import pytest
import yaml

from bench.realworld.checkout import load_projects
from sqlguard.leakage.spec import LeakageSpec

ROUND2 = Path(__file__).resolve().parents[2] / "bench" / "realworld" / "round2"
V3, V4 = ROUND2 / "specs", ROUND2 / "specs-v4"
CITATION = re.compile(r"[\w./-]+\.(?:sql|md|yml|yaml|py|ipynb|json|toml):\d+")
CITED_FIELDS = (
    "output_roles",
    "label_known_after",
    "validity_end_columns",
    "unique_keys",
    "availability_columns",
)
# a field that v4 may add or change in a spec; anything else must equal the old spec
V4_FIELDS = {"output_roles", "label_known_after", "time_columns", "label_columns", "realworld"}
PROJECTS = load_projects(ROUND2 / "projects.yaml")


def comment_block_above(lines: list[str], index: int) -> str:
    block = []
    for line in reversed(lines[:index]):
        if not line.lstrip().startswith("#"):
            break
        block.append(line)
    return "\n".join(reversed(block))


def cited(path: Path, field: str) -> bool:
    """Does the field (top level, or the key of a nested block) have a citation in its comment,
    or in the comments inside its block?"""
    lines = path.read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(f"{field}:"))
    end = start + 1
    while end < len(lines) and (not lines[end] or lines[end][0] in " #"):
        end += 1
    text = comment_block_above(lines, start) + "\n" + "\n".join(lines[start:end])
    return bool(CITATION.search(text))


def test_every_project_has_a_v4_spec():
    assert {p.spec_name for p in PROJECTS} == {path.stem for path in V4.glob("*.yaml")}


@pytest.mark.parametrize("project", PROJECTS, ids=lambda p: p.spec_name)
def test_a_v4_spec_loads_and_cites_every_declared_field(project):
    path = V4 / f"{project.spec_name}.yaml"
    new = yaml.safe_load(path.read_text(encoding="utf-8"))
    if new["realworld"].get("applicable") is False:
        return
    LeakageSpec.from_dict(new)
    for field in CITED_FIELDS:
        if field in new:
            assert cited(path, field), f"{path.name}: {field} has no file:line citation"


@pytest.mark.parametrize("project", PROJECTS, ids=lambda p: p.spec_name)
def test_a_v4_spec_changes_nothing_but_the_v4_fields(project):
    old = yaml.safe_load((V3 / f"{project.spec_name}.yaml").read_text(encoding="utf-8"))
    new = yaml.safe_load((V4 / f"{project.spec_name}.yaml").read_text(encoding="utf-8"))
    changed = {k for k in old.keys() | new.keys() if old.get(k) != new.get(k)}
    assert changed <= V4_FIELDS, (project.spec_name, changed - V4_FIELDS)
    block_old, block_new = old["realworld"], new["realworld"]
    for key in block_old.keys() | block_new.keys():
        if key == "schema":  # only column types may differ
            for table in block_old.get("schema", {}).keys() | block_new.get("schema", {}).keys():
                assert block_old["schema"].keys() == block_new["schema"].keys()
                assert block_old["schema"][table].keys() == block_new["schema"][table].keys()
        else:
            assert block_old.get(key) == block_new.get(key), (project.spec_name, key)


def test_the_gate_rejects_a_role_without_a_citation(tmp_path):
    bad = tmp_path / "s.yaml"
    bad.write_text("label_table: t\noutput_roles:\n  # because I said so\n  label: [y]\n")
    assert not cited(bad, "output_roles")
    good = tmp_path / "g.yaml"
    good.write_text("label_table: t\n# models/a.sql:12 says so\noutput_roles:\n  label: [y]\n")
    assert cited(good, "output_roles")


def test_the_control_v4_spec_changes_only_what_it_cites():
    base = ROUND2.parent
    name = "SarrangTech__fraud-detection-feature-store"
    old = yaml.safe_load((base / "specs" / f"{name}.yaml").read_text(encoding="utf-8"))
    path = base / "round1-specs-v4" / f"{name}.yaml"
    new = yaml.safe_load(path.read_text(encoding="utf-8"))
    LeakageSpec.from_dict(new)
    changed = {k for k in old.keys() | new.keys() if old.get(k) != new.get(k)}
    assert changed == {"time_columns", "availability_columns", "unique_keys"}
    for field in ("availability_columns", "unique_keys"):
        assert cited(path, field), field
    assert old["realworld"] == new["realworld"]
