"""The files a pre-registration freezes, and the check that a run still sees them unchanged.

The pre-registration records the SHA-256 of every file that decides a verdict: the leakage rules and
weights, the dbt inlining, the specs, the split manifest and the world generator. A run on a split
nobody has seen compares the working tree with that record, so a rule edited after the freeze is
caught even when the edit never reached a commit.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FROZEN_GLOBS = (
    "src/sqlguard/leakage/**/*",
    "src/sqlguard/dbt/**/*",
    "bench/leakage/spec.yaml",
    "bench/leakage/spec_holdout_v2.yaml",
    "bench/leakage/splits.yaml",
    "bench/leakage/world.py",
)
# Recorded, not enforced: the author briefs and the tools that gate the authors' output. A run
# reports which of them differ from the record; a change is visible, not a reason to refuse.
RECORDED_GLOBS = (
    "bench/briefs/*.md",
    "bench/leakage/splits.yaml",
    "bench/leakage/audit.py",
    "bench/leakage/duplicates.py",
    "bench/leakage/freeze.py",
)
_IGNORED = ("__pycache__", ".pyc")


def _files(root: Path, globs: tuple[str, ...] = FROZEN_GLOBS) -> list[Path]:
    found = {
        p
        for pattern in globs
        for p in root.glob(pattern)
        if p.is_file() and not any(part in str(p) for part in _IGNORED)
    }
    return sorted(found)


def frozen_hashes(root: Path = ROOT) -> dict[str, str]:
    """`{path relative to the repository: sha256}` for every frozen file."""
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in _files(root)
    }


def recorded_hashes(root: Path = ROOT) -> dict[str, str]:
    """The same for the files that are recorded but not enforced (`RECORDED_GLOBS`)."""
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in _files(root, RECORDED_GLOBS)
    }


def recorded_differences(recorded: Mapping[str, str], root: Path = ROOT) -> list[str]:
    """Changed, missing and added recorded files, in the words of `frozen_mismatches`."""
    now = recorded_hashes(root)
    return (
        [f"changed: {n}" for n in sorted(recorded) if n in now and now[n] != recorded[n]]
        + [f"missing: {n}" for n in sorted(set(recorded) - set(now))]
        + [f"added: {n}" for n in sorted(set(now) - set(recorded))]
    )


def frozen_mismatches(recorded: Mapping[str, str], root: Path = ROOT) -> list[str]:
    """What differs between the record and the tree: changed, missing and added files."""
    now = frozen_hashes(root)
    problems = [
        f"changed: {name}"
        for name in sorted(recorded)
        if name in now and now[name] != recorded[name]
    ]
    problems += [f"missing: {name}" for name in sorted(set(recorded) - set(now))]
    problems += [f"added: {name}" for name in sorted(set(now) - set(recorded))]
    return problems
