"""Run the v1 detector (git tag `v0.1.1`) from its own virtualenv, one subprocess per batch.

The v1 code and its pinned dependencies live in a cache directory outside the repository
(`~/.cache/sqlguard-bench/v1/<commit>/`), built from `git archive` of the tag and its `uv.lock`;
nothing of v1 is imported into the benchmark process. The same file is the worker the virtualenv
runs: `python -I v1_runner.py --worker --in request.json --out results.jsonl`. The worker imports
only `sqlguard` (v1) and the standard library.

What a v1 score means here:
- The spec is the v1 fields only (v1 ignores keys it does not know; the request carries none).
- A query v1 cannot read (it does not parse, or v1 raises, hangs or dies on it) scores 0.0, "not
  detected", and is recorded with the reason. So is a case whose true leak type v1 has no name for
  (L7): v1's template reader rejects it. The score v1 gave before that is kept as `raw_score`.
- Any other `unverifiable` verdict keeps the score v1 gave and is recorded as unverifiable, like
  the other detectors'.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tarfile
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

V1_TAG = "v0.1.1"
V1_LEAK_TYPES = frozenset({"clean", "L1", "L2", "L3", "L4", "L5", "L6"})
# Spec keys the first rule set does not have. A v1 spec is the v2 spec without them.
V2_SPEC_FIELDS = ("boundary", "max_ingest_delay", "label_known_after", "relations")
CASE_TIMEOUT_SECONDS = 300
CACHE_ENV = "SQLGUARD_BENCH_CACHE"
WORKER = Path(__file__).resolve()
_REPO = WORKER.parents[2]
_WORKER_ERROR_EXIT = 3


class V1Error(RuntimeError):
    """The v1 environment could not be built or the worker could not be run."""


def v1_spec_data(data: dict) -> dict:
    """`data` (a parsed spec file) without the fields added after v1."""
    return {k: v for k, v in data.items() if k not in V2_SPEC_FIELDS}


def describe_unverifiable(report: object, unchecked: Sequence[str] | set[str]) -> str:
    """Why a validator report is `unverifiable` (empty for any other verdict).

    Works on the v1 and the v2 report alike: only `verdict`, `findings` and `replay` are read.
    """
    if getattr(report, "verdict", "") != "unverifiable":
        return ""
    messages = [f.message for f in report.findings if f.rule_id in unchecked]  # type: ignore[attr-defined]
    if messages:
        return "; ".join(messages)
    replay = getattr(report, "replay", None)
    if replay is not None and getattr(replay, "reason", ""):
        return replay.reason
    return "a static hint (L4) can only be settled by replay"


# --- worker (runs inside the v1 virtualenv) -------------------------------------------------


@dataclass
class V1Score:
    """What v1 said about one query.

    `reasons` maps `static` / `combined` to why v1 could not settle that detector's verdict (v1's
    `unverifiable`); a detector that is absent was checked. `unreadable` queries are the ones v1
    could not read at all: both scores are 0.0 and both detectors carry the reason.
    """

    static: float
    combined: float
    reasons: dict[str, str]
    leak_types: list[str]
    unreadable: bool = False
    raw_static: float | None = None
    raw_combined: float | None = None

    @classmethod
    def not_detected(cls, reason: str, raw: V1Score | None = None) -> V1Score:
        return cls(
            0.0,
            0.0,
            {"static": reason, "combined": reason},
            [],
            True,
            raw.static if raw else None,
            raw.combined if raw else None,
        )


class _CaseTimeout(Exception):
    pass


def _timed_out(signum: int, frame: object) -> None:
    raise _CaseTimeout


def _score_one(sql: str, spec, schema, replayer, weights, validate, unchecked) -> V1Score:
    static = validate(sql, spec, schema=schema, weights=weights)
    if any(f.rule_id == "PARSE" for f in static.findings):
        message = "; ".join(f.message for f in static.findings if f.rule_id == "PARSE")
        return V1Score.not_detected(f"v1 cannot parse the query: {message}")
    combined = validate(sql, spec, schema=schema, replay=replayer, weights=weights)
    reasons = {
        "static": describe_unverifiable(static, unchecked),
        "combined": describe_unverifiable(combined, unchecked),
    }
    return V1Score(
        static.score,
        combined.score,
        {name: reason for name, reason in reasons.items() if reason},
        combined.leak_types,
    )


def _worker(request_path: Path, out_path: Path) -> int:
    """Score the request's queries with v1, appending one JSON line per query to `out_path`."""
    from sqlguard.core.catalog import LakehouseCatalog
    from sqlguard.leakage.replay import Replayer
    from sqlguard.leakage.spec import LeakageSpec
    from sqlguard.leakage.validator import UNCHECKED, Weights, validate

    request = json.loads(request_path.read_text(encoding="utf-8"))
    spec = LeakageSpec.from_dict(request["spec"])
    catalog = LakehouseCatalog.local(request["warehouse"])
    schema = catalog.schema_for()
    replayer = Replayer(catalog, spec, max_points=24, seed=request["seed"])
    weights = Weights.load()
    timeout = int(request.get("case_timeout", CASE_TIMEOUT_SECONDS))
    signal.signal(signal.SIGALRM, _timed_out)
    with open(out_path, "a", encoding="utf-8") as out:
        for index in range(request["start"], len(request["sqls"])):
            signal.alarm(timeout)
            try:
                score = _score_one(
                    request["sqls"][index], spec, schema, replayer, weights, validate, UNCHECKED
                )
            except _CaseTimeout:
                score = V1Score.not_detected(f"v1 did not finish within {timeout} s")
            except Exception as err:  # noqa: BLE001 - any v1 failure on a query is a result
                score = V1Score.not_detected(f"v1 raised {type(err).__name__}: {err}")
            finally:
                signal.alarm(0)
            out.write(json.dumps({"i": index, **asdict(score)}) + "\n")
            out.flush()
    return 0


# --- host (runs in the benchmark process) -----------------------------------------------------


def cache_dir() -> Path:
    override = os.environ.get(CACHE_ENV)
    return Path(override) if override else Path.home() / ".cache" / "sqlguard-bench" / "v1"


def _run(args: list[str], what: str, **kwargs) -> subprocess.CompletedProcess:
    done = subprocess.run(args, capture_output=True, text=True, check=False, **kwargs)
    if done.returncode:
        tail = (done.stderr or done.stdout).strip().splitlines()[-8:]
        raise V1Error(f"{what} failed ({done.returncode}): " + " | ".join(tail))
    return done


def tag_commit(tag: str = V1_TAG, repo: Path = _REPO) -> str:
    return _run(
        ["git", "rev-parse", f"{tag}^{{commit}}"], f"resolving tag {tag}", cwd=repo
    ).stdout.strip()


def _venv_python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _probe(python: Path, version: str) -> bool:
    """True when `python` imports lakehouse-sql-guard at exactly `version`, from its own
    environment (the repository's package carries the same version number)."""
    code = (
        "import importlib.metadata as m, sqlguard, sys; "
        "print(m.version('lakehouse-sql-guard')); print(sqlguard.__file__); print(sys.prefix)"
    )
    done = subprocess.run(
        [str(python), "-I", "-c", code], capture_output=True, text=True, check=False
    )
    if done.returncode:
        return False
    found, origin, prefix = (done.stdout.strip().splitlines() + ["", "", ""])[:3]
    return found == version and bool(prefix) and origin.startswith(prefix)


def ensure_v1_python(
    tag: str = V1_TAG,
    cache: Path | None = None,
    repo: Path = _REPO,
    log: Callable[[str], None] = print,
) -> Path:
    """The interpreter of the v1 virtualenv, building it first if needed.

    The environment is keyed by the tag's commit, so a moved or re-created tag builds a new one.
    It holds v1's locked dependencies (from its `uv.lock`) and v1 installed from `git archive`.
    """
    commit = tag_commit(tag, repo)
    root = (cache or cache_dir()) / commit
    python = _venv_python(root / "venv")
    ready = root / "ready"
    version = tag.removeprefix("v")
    if ready.is_file() and python.is_file() and _probe(python, version):
        return python
    log(f"building the v1 environment ({tag} = {commit[:10]}) in {root}")
    shutil.rmtree(root, ignore_errors=True)
    source = root / "src"
    source.mkdir(parents=True)
    archive = subprocess.run(
        ["git", "archive", "--format=tar", commit], capture_output=True, check=False, cwd=repo
    )
    if archive.returncode:
        message = archive.stderr.decode(errors="replace")[-300:]
        raise V1Error(f"git archive {commit} failed: {message}")
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
        tar.extractall(source, filter="data")
    interpreter = f"{sys.version_info.major}.{sys.version_info.minor}"
    _run(["uv", "venv", "--python", interpreter, str(root / "venv")], "uv venv")
    locked = _run(
        ["uv", "export", "--frozen", "--no-dev", "--no-emit-project", "--no-hashes"],
        "uv export",
        cwd=source,
    ).stdout
    requirements = root / "requirements.txt"
    requirements.write_text(locked, encoding="utf-8")
    install = ["uv", "pip", "install", "--python", str(python)]
    _run([*install, "-r", str(requirements)], "installing the v1 dependencies")
    _run([*install, "--no-deps", str(source)], "installing v1")
    if not _probe(python, version):
        raise V1Error(f"the environment in {root} does not import lakehouse-sql-guard {version}")
    ready.write_text(commit + "\n", encoding="utf-8")
    return python


def run_v1(
    sqls: Sequence[str],
    *,
    spec_data: dict,
    warehouse: Path,
    seed: int,
    python: Path | None = None,
    work_dir: Path,
    case_timeout: int = CASE_TIMEOUT_SECONDS,
    worker: Path = WORKER,
) -> list[V1Score]:
    """Score `sqls` with v1 against the Iceberg catalog in `warehouse`, one result per query.

    `spec_data` is a parsed spec file; the fields v1 does not know are dropped here. If the worker
    dies on a query (a crash that the worker's own handlers cannot catch), that query is recorded
    as unreadable and the rest continue in a fresh worker.
    """
    python = python or ensure_v1_python()
    work_dir.mkdir(parents=True, exist_ok=True)
    request_path, out_path = work_dir / "request.json", work_dir / "results.jsonl"
    out_path.unlink(missing_ok=True)
    scores: dict[int, V1Score] = {}
    start = 0
    while start < len(sqls):
        request = {
            "warehouse": str(warehouse),
            "seed": seed,
            "spec": v1_spec_data(spec_data),
            "sqls": list(sqls),
            "start": start,
            "case_timeout": case_timeout,
        }
        request_path.write_text(json.dumps(request), encoding="utf-8")
        done = subprocess.run(
            [
                str(python),
                "-I",
                str(worker),
                "--worker",
                "--in",
                str(request_path),
                "--out",
                str(out_path),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        lines = out_path.read_text(encoding="utf-8").splitlines() if out_path.exists() else []
        for line in lines:
            record = json.loads(line)
            index = record.pop("i")
            scores[index] = V1Score(**record)
        reached = len(scores)
        if reached >= len(sqls):
            break
        stderr = " | ".join(done.stderr.strip().splitlines()[-4:])
        if reached == start:  # nothing scored at all: the worker is broken, not one query
            raise V1Error(
                f"the v1 worker failed before scoring anything ({done.returncode}): {stderr}"
            )
        scores[reached] = V1Score.not_detected(
            f"the v1 process died on this query (exit {done.returncode}): {stderr}"
        )
        start = reached + 1
    return [scores[i] for i in range(len(sqls))]


def apply_v1_taxonomy(score: V1Score, truth: str) -> V1Score:
    """Score of a case whose true leak type v1 has no name for (L7): not detected, with the reason.

    v1 reads templates of L1-L6 only, so a leaky L7 case never reaches it in its own benchmark.
    """
    if truth in V1_LEAK_TYPES:
        return score
    return V1Score.not_detected(
        f"v1 has no leak type {truth} (its template reader rejects it)", score
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--worker", action="store_true", required=True)
    parser.add_argument("--in", dest="request", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    return _worker(args.request, args.out)


if __name__ == "__main__":
    sys.exit(main())
