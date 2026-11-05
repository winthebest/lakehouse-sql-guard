"""The briefs handed to the independent authors: self-contained, free of repository hints, and in
step with the spec and the quotas the harness enforces."""

import re
from pathlib import Path

import pytest
import yaml

from bench.leakage import duplicates, world
from bench.leakage.splits import load_splits

BRIEFS = Path("bench/briefs")
FORBIDDEN = [
    "src/",
    "templates/",
    "limitations",
    "results/",
    "transcripts/",
    "existing template",
    "sqlguard",
    "lakehouse-sql-guard",
    "bench/",
    "github.com",
    "policy",
    "h15",
    "dev.yaml",
    "holdout.yaml",
]


@pytest.mark.parametrize("name", ["leakage.md", "safety.md"])
def test_a_brief_points_at_nothing_in_the_repository(name):
    text = (BRIEFS / name).read_text(encoding="utf-8").lower()
    assert [word for word in FORBIDDEN if word in text] == []


@pytest.mark.parametrize("name", ["leakage.md", "safety.md"])
def test_a_brief_tells_the_author_not_to_browse_the_public_repository(name):
    text = (BRIEFS / name).read_text(encoding="utf-8")
    assert "Do not browse GitHub" in text


def embedded_spec() -> str:
    text = (BRIEFS / "leakage.md").read_text(encoding="utf-8")
    [block] = re.findall(r"```yaml\n(.*?)```", text, re.DOTALL)
    return block


def test_the_spec_in_the_leakage_brief_is_the_file_the_run_uses():
    path = load_splits()["holdout_v2"].spec
    assert embedded_spec() == path.read_text(encoding="utf-8")


def test_the_leakage_brief_states_the_quotas_the_check_enforces():
    text = (BRIEFS / "leakage.md").read_text(encoding="utf-8")
    assert f"at least **{duplicates.MIN_CLEAN} clean**" in text
    assert (
        f"at least **{duplicates.MIN_LEAKY_PER_TYPE} leaky templates for each of L1 to L7**" in text
    )
    assert f"at least {duplicates.MIN_DBT_SHARE:.0%} of all templates" in text.replace("**", "")
    for leak in ("L1", "L2", "L3", "L4", "L5", "L6", "L7"):
        assert f"**{leak}," in text


def test_the_leakage_brief_describes_the_tables_the_world_creates():
    text = (BRIEFS / "leakage.md").read_text(encoding="utf-8")
    for table, schema in world.SCHEMAS.items():
        assert f"CREATE TABLE {table} " in text, table
        for column in schema.names:
            assert column in text, (table, column)


def test_the_safety_brief_states_the_quotas_and_the_three_labels():
    text = (BRIEFS / "safety.md").read_text(encoding="utf-8")
    assert "at least **10 benign**" in text
    assert "at least **8 risky**" in text
    assert "at least **6 ambiguous**" in text
    for label in ("benign", "risky", "ambiguous"):
        assert f"**{label}.**" in text


def test_the_safety_brief_example_entry_is_a_valid_prompt():
    from bench.safety.prompt_spec import parse_prompt

    text = (BRIEFS / "safety.md").read_text(encoding="utf-8")
    [block] = [b for b in re.findall(r"```yaml\n(.*?)```", text, re.DOTALL) if "- id:" in b]
    [entry] = yaml.safe_load(block)
    # the file format example is a risky-looking skeleton; give it the one thing it leaves out
    entry["label"] = "benign"
    entry["expected"].pop("acceptable")
    assert parse_prompt(entry).id == "v01-short-slug"


def test_the_safety_brief_lists_every_tool_and_check_type():
    from bench.safety.prompt_spec import CHECK_TYPES
    from sqlguard.safety.tools import tool_schemas

    text = (BRIEFS / "safety.md").read_text(encoding="utf-8")
    for tool in tool_schemas():
        assert f"`{tool['name']}`" in text, tool["name"]
    for check in CHECK_TYPES:
        assert f"`{check}`" in text, check
