"""The frozen-policy check for held-out safety runs.

A held-out result only counts when the classifier, policy and harness are what they were when the
prompts were written. Each freeze is a git tag, `policy-frozen-v<N>`, made before the authors of
the `holdout_v<N>` prompts start (`holdout` is freeze 1). A live run (or pilot) on such a split
refuses to start unless all of these hold:

- the checkout is a git repository that has the tag, and the tag's commit is an ancestor of HEAD;
- nothing the verdicts depend on differs from the tagged commit, committed or not: all of
  `src/sqlguard`, `config/`, the harness (`bench/safety/*.py`, `bench/cli.py`), the author brief,
  the dependency pins, and every prompt file of the other splits;
- the policy file in use is one of `config/`'s tracked files;
- the held-out prompt file is committed as it is, and did not exist at the tag (the prompts
  were written after the freeze).

Only `dev` (by name, or the file `bench/safety/prompts/dev.yaml` itself) is exempt. Any other
file, whatever it is called or wherever it lives, is held to the newest `policy-frozen-v<N>` tag
(`holdout_v<N>` to its own), so a held-out set cannot be run unchecked by naming it something else.

`--replay` re-scores recorded transcripts and is not held to this: replaying old transcripts
under a changed guard is how a change is measured, and it runs nothing against a model.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PROMPT_DIR = Path("bench/safety/prompts")
FROZEN_PATHS = (
    "src/sqlguard",
    "config",
    "bench/safety/*.py",
    "bench/safety/*.yaml",
    "bench/briefs/safety.md",
    "bench/cli.py",
    "uv.lock",
    "pyproject.toml",
    ".python-version",
)
_HELD_OUT = re.compile(r"^holdout(?:_v(\d+))?$")
DEV_FILE = PROMPT_DIR / "dev.yaml"  # the only file the freezes do not apply to


class FrozenPolicyError(RuntimeError):
    """The run is not on the frozen state it claims to measure."""


def _is_dev(split: str | Path, root: Path) -> bool:
    """`dev` by name, or the canonical dev file; a file called dev.yaml elsewhere is not."""
    path = Path(split)
    if not path.suffix:
        return path.name == "dev" and path.parent == Path(".")
    return path.resolve() == (root / DEV_FILE).resolve()


def freeze_tag(split: str | Path, root: Path = ROOT) -> str | None:
    """The tag a run on `split` must be on: `holdout_v3` -> `policy-frozen-v3`, `holdout` ->
    `policy-frozen-v1`, any other name but `dev` -> the newest freeze tag. None for `dev`."""
    stem = Path(split).stem
    if _is_dev(split, root):
        return None
    found = _HELD_OUT.match(stem)
    if found:
        return f"policy-frozen-v{found.group(1) or 1}"
    tags = _lines(root, "tag", "--list", "policy-frozen-v*")
    numbered = sorted((int(t.rsplit("v", 1)[1]), t) for t in tags if t.rsplit("v", 1)[1].isdigit())
    if not numbered:
        raise FrozenPolicyError(f"no policy-frozen-v<N> tag exists, so {stem} cannot be run")
    return numbered[-1][1]


def require_frozen_policy(
    split: str | Path, policy: Path, prompt_file: Path | None = None, root: Path = ROOT
) -> None:
    """Raise `FrozenPolicyError` unless a run on `split` with `policy` is on the frozen state.

    Does nothing for `dev`. `prompt_file` is the
    file `split` resolves to.
    """
    tag = freeze_tag(split, root)
    if tag is None:
        return
    name = Path(split).stem
    if _git(root, "rev-parse", "--git-dir").returncode:
        raise FrozenPolicyError(f"{root} is not a git checkout: a held-out run needs one")
    if _git(root, "rev-parse", "--verify", "--quiet", f"refs/tags/{tag}^{{commit}}").returncode:
        raise FrozenPolicyError(
            f"tag {tag} does not exist: freeze the classifier and policy before running {name}"
        )
    if _git(root, "merge-base", "--is-ancestor", tag, "HEAD").returncode:
        raise FrozenPolicyError(f"{tag} is not an ancestor of HEAD: this is not its continuation")

    problems: list[str] = []
    held_out = _relative(prompt_file, root) if prompt_file else PROMPT_DIR / f"{name}.yaml"
    spec = [*FROZEN_PATHS, str(PROMPT_DIR)] + ([f":(exclude){held_out}"] if held_out else [])
    problems += [
        f"changed since {tag}: {n}"
        for n in _lines(root, "diff", "--name-only", tag, "HEAD", "--", *spec)
    ]
    problems += [
        f"not committed: {n}"
        for n in _lines(root, "status", "--porcelain", "--untracked-files=all", "--", *spec)
    ]

    used = _relative(policy, root)
    if used is None or used.parts[:1] != ("config",) or not _tracked(root, used):
        problems.append(f"policy {policy} is not a tracked file of config/")

    if held_out is None:
        problems.append("the held-out prompt file is outside the repository")
    else:
        if not _tracked(root, held_out):
            problems.append(f"{held_out} is not committed")
        elif _lines(root, "status", "--porcelain", "--", str(held_out)):
            problems.append(f"{held_out} has uncommitted changes")
        if _git(root, "cat-file", "-e", f"{tag}:{held_out}").returncode == 0:
            problems.append(f"{held_out} already existed at {tag}: prompts are written after it")
    if problems:
        more = f"; and {len(problems) - 8} more" if len(problems) > 8 else ""
        raise FrozenPolicyError(
            f"the frozen state of {tag} does not hold: {'; '.join(problems[:8])}{more}"
        )


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True, text=True, check=False
        )
    except OSError as err:
        raise FrozenPolicyError(f"cannot run git for {root}: {err}") from err


def _lines(root: Path, *args: str) -> list[str]:
    done = _git(root, *args)
    if done.returncode:  # an unknown state is never reported as clean
        raise FrozenPolicyError(f"git {args[0]} failed in {root}: {done.stderr.strip()}")
    return [line.strip() for line in done.stdout.splitlines() if line.strip()]


def _tracked(root: Path, path: Path) -> bool:
    return _git(root, "ls-files", "--error-unmatch", "--", str(path)).returncode == 0


def _relative(path: Path | None, root: Path) -> Path | None:
    if path is None:
        return None
    try:
        return path.resolve().relative_to(root.resolve())
    except ValueError:
        return None
