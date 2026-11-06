"""Clone a project at its pinned commit and compile it with dbt-duckdb into a manifest.

Clones live in the git-ignored `.realworld/` directory. dbt gets a throwaway in-memory DuckDB
profile (no credentials, no data): only the compiled SQL in `manifest.json` is needed. The output
directory is `build_<slug>` inside the dbt project, never the project's own `target-path`.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORK = ROOT / ".realworld"
BUILD_PREFIX = "build_"
DBT_TIMEOUT_SECONDS = 600


class CheckoutError(RuntimeError):
    """The pinned commit could not be fetched, or dbt could not compile the project."""


@dataclass(frozen=True)
class Project:
    repo: str
    group: str
    sha: str
    license: str
    publish: str
    dbt_subdir: str = "."
    default_branch: str = ""
    # a modified copy of the project (a mutant) keeps the repo but has a clone of its own
    variant: str = ""

    @property
    def slug(self) -> str:
        """`owner__repo`, the name used for the clone, the spec and the build directory."""
        base = self.repo.replace("/", "__")
        return f"{base}__{self.variant}" if self.variant else base

    @property
    def spec_name(self) -> str:
        """The spec file of the project (a variant is checked with the spec of its project)."""
        return self.repo.replace("/", "__")

    @property
    def url(self) -> str:
        return f"https://github.com/{self.repo}.git"

    @property
    def clone(self) -> Path:
        return WORK / self.slug

    @property
    def dbt_dir(self) -> Path:
        return self.clone / self.dbt_subdir

    @property
    def build_name(self) -> str:
        return f"{BUILD_PREFIX}{self.slug}"

    @property
    def manifest(self) -> Path:
        return self.dbt_dir / self.build_name / "manifest.json"


def load_projects(path: Path | str) -> list[Project]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    projects = []
    for entry in data["projects"]:
        sha = str(entry["sha"])
        if len(sha) != 40 or any(c not in "0123456789abcdef" for c in sha):
            raise ValueError(f"{entry['repo']}: sha must be a full 40-character commit id")
        if entry["group"] not in ("new", "seen"):
            raise ValueError(f"{entry['repo']}: group must be new or seen")
        projects.append(
            Project(
                repo=entry["repo"],
                group=entry["group"],
                sha=sha,
                license=str(entry["license"]),
                publish=entry["publish"],
                dbt_subdir=str(entry.get("dbt_subdir", ".")),
                default_branch=str(entry.get("default_branch", "")),
            )
        )
    return projects


def _git(args: list[str], cwd: Path) -> str:
    done = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=DBT_TIMEOUT_SECONDS
    )
    if done.returncode != 0:
        raise CheckoutError(f"git {' '.join(args)} failed in {cwd}: {done.stderr.strip()[:300]}")
    return done.stdout.strip()


def ensure_checkout(project: Project) -> None:
    """The project's files at exactly the pinned commit (shallow fetch by SHA)."""
    clone = project.clone
    if not clone.is_dir():
        clone.mkdir(parents=True)
        _git(["init", "-q"], clone)
        _git(["remote", "add", "origin", project.url], clone)
    try:
        head = _git(["rev-parse", "HEAD"], clone)
    except CheckoutError:
        head = ""
    if head != project.sha:
        _git(["fetch", "-q", "--depth", "1", "origin", project.sha], clone)
        _git(["checkout", "-q", "--force", project.sha], clone)
    if _git(["rev-parse", "HEAD"], clone) != project.sha:
        raise CheckoutError(f"{project.repo}: checkout is not at {project.sha}")


def profile_name(project: Project) -> str:
    config = yaml.safe_load((project.dbt_dir / "dbt_project.yml").read_text(encoding="utf-8"))
    name = config.get("profile")
    if not name:
        raise CheckoutError(f"{project.repo}: dbt_project.yml names no profile")
    return str(name)


def write_profile(project: Project) -> Path:
    """A throwaway in-memory DuckDB profile under the git-ignored work directory."""
    directory = WORK / "profiles" / project.slug
    directory.mkdir(parents=True, exist_ok=True)
    profile = {
        profile_name(project): {
            "target": "sqlguard",
            "outputs": {"sqlguard": {"type": "duckdb", "path": ":memory:", "threads": 1}},
        }
    }
    (directory / "profiles.yml").write_text(yaml.safe_dump(profile), encoding="utf-8")
    return directory


def _dbt(args: list[str], project: Project, profiles: Path) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "DBT_SEND_ANONYMOUS_USAGE_STATS": "false",
        "DO_NOT_TRACK": "1",
        "DBT_PROFILES_DIR": str(profiles),
    }
    executable = str(Path(sys.executable).parent / "dbt")
    return subprocess.run(
        [executable, *args, "--profiles-dir", str(profiles), "--no-version-check"],
        cwd=project.dbt_dir,
        env=env,
        capture_output=True,
        text=True,
        timeout=DBT_TIMEOUT_SECONDS,
    )


def compile_project(project: Project, deps: bool = True) -> dict[str, Any]:
    """`dbt deps` (if the project has packages) and `dbt compile` into `build_<slug>`.

    Returns what happened. dbt exits non-zero when some model does not compile; the manifest is
    still written, and the check reports per training model what it lacks.
    """
    profiles = write_profile(project)
    has_packages = any(
        (project.dbt_dir / f).is_file() for f in ("packages.yml", "dependencies.yml")
    )
    ran_deps = False
    if deps and has_packages:
        installed = _dbt(["deps"], project, profiles)
        if installed.returncode != 0:
            raise CheckoutError(f"{project.repo}: dbt deps failed: {installed.stdout[-400:]}")
        ran_deps = True
    done = _dbt(
        [
            "compile",
            "--target-path",
            project.build_name,
            "--log-path",
            f"{project.build_name}/logs",
        ],
        project,
        profiles,
    )
    if not project.manifest.is_file():
        raise CheckoutError(f"{project.repo}: dbt compile wrote no manifest: {done.stdout[-600:]}")
    return {
        "compile_exit": done.returncode,
        "compile_tail": "" if done.returncode == 0 else done.stdout[-600:],
        "deps": ran_deps,
    }
