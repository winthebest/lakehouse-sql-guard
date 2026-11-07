"""The blind reader of the third dbt search: the file it writes and the rule that matches its lines
to checker findings.

A separate agent reads each project's training model and documents before any checker output
exists, and writes the leaks it suspects to `blind-read.csv`. After the run, each reader line is
judged true or false by two blind reviewers, as a finding is. A line judged true that matches no
finding is a leak the checker missed; recall by the reader is the share of true lines matched. The
reader can miss leaks too, so the figure is a ceiling on what is known, not recall of all leaks.

A reader line matches a finding when ALL hold:

1. same project and same training model;
2. the leak types are in the same group (`LEAK_GROUPS`);
3. the finding's location is the line's `source` (compared by the last part of the name, without
   regard to case), OR the line's `feature` appears in the finding's message or evidence as a whole
   word (letters, digits and underscores do not extend it).
"""

from __future__ import annotations

import csv
import re
from collections.abc import Mapping
from pathlib import Path

from bench.leakage.metrics import clopper_pearson_one_sided

READER_COLUMNS = ("id", "project", "model", "feature", "source", "leak_type", "evidence", "note")
# Types that are one mistake read differently: a bound that is missing, wrong or off at the edge.
LEAK_GROUPS = {
    "L1": "bound",
    "L2": "bound",
    "L3": "bound",
    "L5": "bound",
    "L4": "late",
    "L6": "label",
    "L7": "clock",
}


def group_of(leak_type: str) -> str:
    try:
        return LEAK_GROUPS[leak_type.strip().upper()]
    except KeyError:
        raise ValueError(f"unknown leak type {leak_type!r}: use {sorted(LEAK_GROUPS)}") from None


def read_reader_file(path: Path) -> list[dict[str, str]]:
    """The reader's lines. Refuses other columns, an unknown leak type or a repeated id."""
    with open(path, newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != READER_COLUMNS:
            raise ValueError(f"{path}: columns must be {list(READER_COLUMNS)}")
        rows = [dict(row) for row in reader]
    seen: set[str] = set()
    for row in rows:
        if row["id"] in seen:
            raise ValueError(f"{path}: id {row['id']!r} appears twice")
        seen.add(row["id"])
        group_of(row["leak_type"])
    return rows


def _last_part(name: str) -> str:
    return name.strip().strip('"').rsplit(".", 1)[-1].lower()


def _mentions(text: str, word: str) -> bool:
    return (
        bool(word)
        and re.search(rf"(?<!\w){re.escape(word)}(?!\w)", text, re.IGNORECASE) is not None
    )


def matches(line: Mapping[str, str], finding: Mapping[str, str]) -> bool:
    """Whether a reader line and a finding are about the same leak (see the module docstring)."""
    if line["project"] != finding["project"] or line["model"] != finding["training_model"]:
        return False
    if group_of(line["leak_type"]) != group_of(finding["rule"]):
        return False
    source = _last_part(line.get("source", ""))
    if source and source == _last_part(finding.get("location", "")):
        return True
    text = f"{finding.get('message', '')} {finding.get('evidence', '')}"
    return _mentions(text, line.get("feature", "").strip())


def reader_recall(
    lines: list[Mapping[str, str]],
    findings: list[Mapping[str, str]],
    labels: Mapping[str, str],
) -> dict:
    """Misses and recall by the reader. `labels` is the final label of every reader line.

    Only lines judged `true` count as leaks the checker should have found; `true_not_actionable`
    lines are counted apart. `lower95` is the one-sided 95% Clopper-Pearson lower bound.
    """
    missing = [line["id"] for line in lines if line["id"] not in labels]
    if missing:
        raise ValueError(f"no adjudicated label for reader line(s) {missing}")
    true = [line for line in lines if labels[line["id"]] == "true"]
    matched = [line for line in true if any(matches(line, f) for f in findings)]
    n, k = len(true), len(matched)
    return {
        "true_lines": n,
        "matched": k,
        "missed": [line["id"] for line in true if line not in matched],
        "recall_by_reader": k / n if n else None,
        "lower95": clopper_pearson_one_sided(k, n, side="lower") if n else None,
        "not_actionable_lines": sum(labels[line["id"]] == "true_not_actionable" for line in lines),
        "false_lines": sum(labels[line["id"]] == "false" for line in lines),
        "unsure_lines": sum(labels[line["id"]] == "unsure" for line in lines),
    }
