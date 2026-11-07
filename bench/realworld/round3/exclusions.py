"""Repositories the third dbt search may never admit.

A repository is excluded when it was screened in round 1 or round 2 (whatever the verdict), when
it appeared in the results of a search of either round (screened or not: its name and description
were shown then), when it was read during development, or when either pre-registration lists it as
excluded. Names are compared exactly, without regard to case; whether a candidate is a fork or a
copy of an excluded project is judged by the screener, not guessed here.

Usage: python -m bench.realworld.round3.exclusions OWNER/REPO ...   (lists the excluded ones)
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
# (why a repository from this file is excluded, path relative to the repository root)
SOURCES = (
    ("round1 screening", "bench/realworld/screening.csv"),
    ("round2 screening", "bench/realworld/round2/screening.csv"),
    ("round1 search hit", "bench/realworld/search-results.json"),
    ("round2 search hit", "bench/realworld/round2/search-results.json"),
    ("round1 preregistration", "bench/realworld/search-prereg.yaml"),
    ("round2 preregistration", "bench/realworld/round2/search-prereg.yaml"),
)
_REPO = re.compile(r"^[\w.-]+/[\w.-]+$")


def _screening(path: Path) -> set[str]:
    with open(path, newline="", encoding="utf-8") as handle:
        return {row["repo"].strip() for row in csv.DictReader(handle) if row.get("repo")}


def _search_hits(path: Path) -> set[str]:
    """Repositories in a saved search: `fullName` for a repository search, `repository.
    nameWithOwner` for a code search."""
    data = json.loads(path.read_text(encoding="utf-8"))
    found = set()
    for search in data["searches"]:
        for result in search.get("results") or []:
            name = result.get("fullName") or (result.get("repository") or {}).get("nameWithOwner")
            if name:
                found.add(name.strip())
    return found


def _preregistered(path: Path) -> set[str]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return {e.strip() for e in data.get("excluded") or [] if _REPO.match(str(e).strip())}


_READERS = {
    ".csv": _screening,
    ".json": _search_hits,
    ".yaml": _preregistered,
}


def load_exclusions(root: Path = ROOT) -> dict[str, list[str]]:
    """`{lowercase owner/repo: [why, ...]}` for every excluded repository."""
    excluded: dict[str, list[str]] = defaultdict(list)
    for why, relative in SOURCES:
        path = root / relative
        for name in _READERS[path.suffix](path):
            reasons = excluded[name.lower()]
            if why not in reasons:
                reasons.append(why)
    return dict(excluded)


def digests(root: Path = ROOT) -> dict[str, str]:
    """`{path: sha256}` of every file the exclusion is read from, for the registration."""
    return {rel: hashlib.sha256((root / rel).read_bytes()).hexdigest() for _, rel in SOURCES}


def excluded_among(candidates: list[str], root: Path = ROOT) -> list[str]:
    """The candidates (as given) that are excluded."""
    excluded = load_exclusions(root)
    return [c for c in candidates if c.strip().lower() in excluded]


def main(argv: list[str] | None = None) -> int:
    names = sys.argv[1:] if argv is None else argv
    excluded = load_exclusions()
    hits = [n for n in names if n.strip().lower() in excluded]
    for name in hits:
        print(f"{name}: excluded ({'; '.join(excluded[name.strip().lower()])})")
    return 1 if hits else 0


if __name__ == "__main__":
    raise SystemExit(main())
