"""The files a pre-registration freezes, and the check that a run still sees them unchanged.

The pre-registration records the SHA-256 of everything that decides a verdict or builds the cases:
all of `src/sqlguard`, the leakage harness (`bench/leakage/*.py`, every YAML there: specs, split
manifest), the author briefs, and the template directories of the splits seen so far. A run on a
split nobody has seen compares the working tree with that record and the checkout with the commit
the pre-registration was written at, so a rule, a parser or a harness edited after the freeze is
caught whether or not it reached a commit, and whatever the working directory is.

Every git command here runs as `git -C <repository root>`: outside a checkout the check refuses
instead of reporting a clean tree.
"""

from __future__ import annotations

import hashlib
import subprocess
from collections.abc import Iterable, Mapping
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FROZEN_GLOBS = (
    "src/sqlguard/**/*",
    "bench/leakage/*.py",
    "bench/leakage/**/*.yaml",
    "bench/briefs/*.md",
)
_IGNORED = ("__pycache__", ".pyc")


def template_globs(splits: Iterable[str]) -> tuple[str, ...]:
    """The template directories of the splits that have been seen (held-out ones come later)."""
    return tuple(f"bench/leakage/templates/{name}/*.sql" for name in sorted(set(splits)))


def _files(root: Path, splits: Iterable[str]) -> list[Path]:
    found = {
        p
        for pattern in (*FROZEN_GLOBS, *template_globs(splits))
        for p in root.glob(pattern)
        if p.is_file() and not any(part in str(p) for part in _IGNORED)
    }
    return sorted(found)


def frozen_hashes(root: Path = ROOT, splits: Iterable[str] = ()) -> dict[str, str]:
    """`{path relative to the repository: sha256}` for every frozen file.

    `splits` are the seen splits whose template files are frozen with the rest.
    """
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in _files(root, splits)
    }


def frozen_mismatches(
    recorded: Mapping[str, str], root: Path = ROOT, splits: Iterable[str] = ()
) -> list[str]:
    """What differs between the record and the tree: changed, missing and added files."""
    now = frozen_hashes(root, splits)
    problems = [
        f"changed: {name}"
        for name in sorted(recorded)
        if name in now and now[name] != recorded[name]
    ]
    problems += [f"missing: {name}" for name in sorted(set(recorded) - set(now))]
    problems += [f"added: {name}" for name in sorted(set(now) - set(recorded))]
    return problems


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=False
    )


def checkout_state(root: Path = ROOT) -> tuple[str, bool]:
    """`(short HEAD sha, working tree has uncommitted changes)` of the checkout at `root`.

    Refuses (SystemExit) when `root` is not a git checkout or git cannot be run: an unknown
    state is never reported as clean.
    """
    try:
        head = _git(root, "rev-parse", "--short", "HEAD")
    except OSError as err:
        raise SystemExit(f"cannot run git for {root}: {err}") from err
    if head.returncode or not head.stdout.strip():
        raise SystemExit(f"{root} is not a git checkout: a held-out run needs one")
    status = _git(root, "status", "--porcelain", "--untracked-files=no")
    if status.returncode:
        raise SystemExit(f"git status failed in {root}: {status.stderr.strip()}")
    return head.stdout.strip(), bool(status.stdout.strip())


def is_ancestor(sha: str, root: Path = ROOT) -> bool:
    """Whether commit `sha` is an ancestor of (or equal to) HEAD. Refuses an unknown commit."""
    known = _git(root, "rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}")
    if known.returncode:
        raise SystemExit(
            f"the pre-registration names commit {sha}, which this checkout does not have"
        )
    return _git(root, "merge-base", "--is-ancestor", sha, "HEAD").returncode == 0
