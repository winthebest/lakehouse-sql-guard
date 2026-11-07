"""The files a pre-registration freezes, and the check that a run still sees them unchanged.

The pre-registration records the SHA-256 of everything that decides a verdict or builds the cases:
all of `src/sqlguard`, the leakage harness (`bench/leakage/*.py`, every YAML there: specs, split
manifest), the benchmark entry point, the author briefs, the dependency pins (`uv.lock`,
`pyproject.toml`), and the template directories of the splits seen so far. A run on a
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

from bench.leakage.metrics import clopper_pearson_one_sided

ROOT = Path(__file__).resolve().parents[2]
FROZEN_GLOBS = (
    "src/sqlguard/**/*",
    "bench/cli.py",  # the entry point of the benchmark
    "bench/leakage/*.py",
    "bench/leakage/**/*.yaml",
    "bench/briefs/*.md",
    # the registered searches for the held-out sets from real projects and documented leaks
    "bench/realworld/round3/search-prereg.yaml",
    "bench/docleaks/search-prereg.yaml",
    # the environment the verdicts were computed in: sqlglot and duckdb versions decide them
    "uv.lock",
    "pyproject.toml",
    ".python-version",
)
TEMPLATES = "bench/leakage/templates/"
_IGNORED = ("__pycache__", ".pyc")


def template_globs(splits: Iterable[str]) -> tuple[str, ...]:
    """The template directories of the splits that have been seen (held-out ones come later)."""
    return tuple(f"bench/leakage/templates/{name}/*.sql" for name in sorted(set(splits)))


def _files(root: Path, splits: Iterable[str]) -> list[Path]:
    """The frozen files. Under `templates/` only the `.sql` files of the given splits count: a
    file the held-out authors add there (even a YAML) is not part of the freeze."""
    found = {
        p
        for pattern in FROZEN_GLOBS
        for p in root.glob(pattern)
        if p.is_file() and TEMPLATES not in p.relative_to(root).as_posix()
    }
    found |= {p for pattern in template_globs(splits) for p in root.glob(pattern) if p.is_file()}
    return sorted(p for p in found if not any(part in str(p) for part in _IGNORED))


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


def template_changes(split: str, root: Path = ROOT) -> list[str]:
    """Files of the split's template directory that git does not have as committed.

    Untracked, modified, staged and deleted files all count (`--untracked-files=all`): the run
    globs every `*.sql` there, so a file that is not in a commit would be scored without a trace.
    """
    status = _git(
        root,
        "status",
        "--porcelain",
        "--untracked-files=all",
        "--",
        f"bench/leakage/templates/{split}",
    )
    if status.returncode:
        raise SystemExit(f"git status failed in {root}: {status.stderr.strip()}")
    return [line.strip() for line in status.stdout.splitlines() if line.strip()]


def digest(hashes: Mapping[str, str]) -> str:
    """One SHA-256 over a split's `{file: sha256}` list, for a header line."""
    joined = "\n".join(f"{name} {sha}" for name, sha in sorted(hashes.items()))
    return hashlib.sha256(joined.encode()).hexdigest()


# --- targets on one-sided exact bounds ------------------------------------------------------------
# A target is judged on the 95% one-sided Clopper-Pearson bound, not on the point estimate: a
# false positive rate (`fpr`) target is met when the upper bound is at most the target, a `recall`
# or `precision` target when the lower bound is at least the target. `errors` are the false
# positives (fpr, precision) or the misses (recall); `n` is the clean units, the leaky units, or
# the flagged units. The bound is compared unrounded.

TARGET_KINDS = ("fpr", "recall", "precision")
_SEARCH_LIMIT = 100_000


def _check_target(kind: str, threshold: float, level: float) -> None:
    if kind not in TARGET_KINDS:
        raise ValueError(f"target kind must be one of {TARGET_KINDS}, got {kind!r}")
    if not 0.0 < threshold < 1.0:
        raise ValueError(f"target threshold must lie strictly between 0 and 1, got {threshold}")
    if not 0.0 < level < 1.0:
        raise ValueError(f"level must lie strictly between 0 and 1, got {level}")


def target_met(kind: str, errors: int, n: int, threshold: float, level: float = 0.95) -> bool:
    """Whether `errors` among `n` units meet the target on the one-sided exact bound."""
    _check_target(kind, threshold, level)
    if n <= 0:
        return False
    if kind == "fpr":
        return clopper_pearson_one_sided(errors, n, level, "upper") <= threshold
    return clopper_pearson_one_sided(n - errors, n, level, "lower") >= threshold


def max_errors_allowed(kind: str, n: int, threshold: float, level: float = 0.95) -> int:
    """The most errors `n` units can contain and still meet the target; -1 if none would."""
    _check_target(kind, threshold, level)
    if n < 0:
        raise ValueError(f"n must not be negative, got {n}")
    allowed = -1
    for errors in range(n + 1):
        if not target_met(kind, errors, n, threshold, level):
            break  # the bound only worsens as errors grow
        allowed = errors
    return allowed


def min_sample_size(kind: str, errors: int, threshold: float, level: float = 0.95) -> int:
    """The smallest number of units for which `errors` errors still meet the target."""
    _check_target(kind, threshold, level)
    if errors < 0:
        raise ValueError(f"errors must not be negative, got {errors}")
    for n in range(max(errors, 1), _SEARCH_LIMIT):
        if target_met(kind, errors, n, threshold, level):
            return n
    raise ValueError(f"no sample size up to {_SEARCH_LIMIT} meets {kind} {threshold}")


def describe_target(
    kind: str, n: int, threshold: float, level: float = 0.95, *, errors_listed: int = 3
) -> dict:
    """What a pre-registration records for a target: the bound, the size, the errors it allows
    and the smallest size for 0..`errors_listed` errors."""
    _check_target(kind, threshold, level)
    return {
        "kind": kind,
        "threshold": threshold,
        "bound": "upper" if kind == "fpr" else "lower",
        "level": level,
        "n": n,
        "max_errors": max_errors_allowed(kind, n, threshold, level),
        "min_n_by_errors": {
            str(k): min_sample_size(kind, k, threshold, level) for k in range(errors_listed + 1)
        },
    }
