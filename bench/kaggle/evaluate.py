"""Score the frozen leakage checker (release v0.2.0) on the Kaggle leak cases.

    uv run python -m bench.kaggle.evaluate [--jobs N] [--out-dir DIR] [--work-dir DIR] [--dry-run]

The protocol is bench/kaggle/eval-prereg.yaml; this file implements it and refuses to run when the
pre-registration, the cases or the checker are not as registered. Every query is checked twice
through the CLI, exactly as a user runs it: statically (`--schema`) and with point-in-time replay
(`--warehouse --replay`) over an Iceberg warehouse built from the case's synthetic rows.

Nothing in a case is edited. The only conversions are mechanical:

- `convert_schema`: schema.yaml (`tables.<t>.columns`) to the flat `{table: {column: TYPE}}` mapping
  the `--schema` flag reads;
- `arrow_type`, `to_datetime` and `plan_commits`: the rows of a case as Iceberg commits in time
  order.

What the checker cannot express (a spec it rejects, a time column it cannot replay) is recorded as
the outcome it produced: error, unverifiable, or a verdict.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
import subprocess
import sys
import tempfile
from collections import Counter
from collections.abc import Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import duckdb
import pyarrow as pa
import yaml

from bench.leakage.metrics import bootstrap_ci
from sqlguard.core.catalog import LOGICAL_TS_PROPERTY, LakehouseCatalog
from sqlguard.leakage.validator import UNCHECKED

ROOT = Path(__file__).resolve().parents[2]
CASES_DIR = ROOT / "bench" / "kaggle" / "cases"
PREREG_PATH = ROOT / "bench" / "kaggle" / "eval-prereg.yaml"
RESULTS_DIR = ROOT / "results" / "kaggle"
EPOCH = datetime(1970, 1, 1)
DAY = timedelta(days=1)

CLASSES = ("leaky", "clean", "unverifiable", "error", "static_only")
DECIDED = ("leaky", "clean")
RULES = (
    "verdict:static",
    "verdict:combined",
    "threshold:static",
    "threshold:replay",
    "threshold:combined",
)
MODES = (
    "static",
    "replay",
)  # "replay" is the run with --replay: its verdict and score are combined


class BuildError(Exception):
    """A case whose warehouse cannot be built under the registered rules (it stays static-only)."""


class ConversionError(Exception):
    """A schema.yaml that cannot be converted to the checker's format."""


# --- conversions ---------------------------------------------------------------------------------


def convert_schema(schema: dict) -> dict[str, dict[str, str]]:
    """schema.yaml to the `--schema` format: `{table: {column: TYPE}}`.

    Type strings, table order and column order are kept. Everything else in schema.yaml
    (event_time, time_unit, availability_time, spine) is not part of the checker's format.
    """
    tables = schema.get("tables") if isinstance(schema, dict) else None
    if not isinstance(tables, dict) or not tables:
        raise ConversionError("schema.yaml has no `tables` mapping")
    converted: dict[str, dict[str, str]] = {}
    for name, table in tables.items():
        columns = table.get("columns") if isinstance(table, dict) else None
        if not isinstance(columns, dict) or not columns:
            raise ConversionError(f"table {name!r} has no `columns` mapping")
        converted[str(name)] = {str(col): str(typ) for col, typ in columns.items()}
    return converted


_SQL_TO_ARROW: dict[str, pa.DataType] = {
    "TINYINT": pa.int32(),
    "SMALLINT": pa.int32(),
    "INTEGER": pa.int32(),
    "BIGINT": pa.int64(),
    "FLOAT": pa.float32(),
    "DOUBLE": pa.float64(),
    "BOOLEAN": pa.bool_(),
    "VARCHAR": pa.string(),
    "DATE": pa.date32(),
    "TIMESTAMP": pa.timestamp("us"),
}
_DECIMAL = re.compile(r"DECIMAL\(\s*(\d+)\s*,\s*(\d+)\s*\)")
INTEGER_TYPES = ("TINYINT", "SMALLINT", "INTEGER", "BIGINT")
_UNIT_KEYWORD = {"s": "seconds", "ms": "milliseconds"}


def arrow_type(sql_type: str) -> pa.DataType:
    """The Arrow type a schema.yaml type is stored as in the warehouse (see the registration)."""
    name = str(sql_type).strip().upper()
    if match := _DECIMAL.fullmatch(name):
        return pa.decimal128(int(match[1]), int(match[2]))
    try:
        return _SQL_TO_ARROW[name]
    except KeyError:
        raise BuildError(f"unsupported column type {sql_type!r}") from None


def to_datetime(value: object, sql_type: str, unit: str | None) -> datetime:
    """A time column value as a naive UTC datetime: TIMESTAMP as is, DATE at midnight, integers
    as the Unix epoch plus the value in `unit` (`s` or `ms`)."""
    if value is None:
        raise BuildError("null in a time column")
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    if str(sql_type).strip().upper() in INTEGER_TYPES and isinstance(value, int):
        keyword = _UNIT_KEYWORD.get(unit or "")
        if keyword is None:
            raise BuildError(f"integer time column without a time_unit of s or ms (got {unit!r})")
        try:
            return EPOCH + timedelta(**{keyword: value})
        except OverflowError:
            raise BuildError(f"integer time value {value} is out of range") from None
    raise BuildError(f"cannot read {value!r} ({sql_type}) as a time")


@dataclass(frozen=True)
class CommitPlan:
    table: str
    logical_ts: datetime
    rows: tuple[int, ...] | None  # row indices in the table; None = every row


def table_roles(schema: dict, spec: dict) -> dict[str, str]:
    """`label`, `static`, `outcome` or `temporal` for every table of the schema."""
    label = str(schema["spine"]["table"]).lower()
    static = {str(t).lower() for t in spec.get("static_tables") or []}
    outcome = {str(ref).split(".", 1)[0].lower() for ref in spec.get("outcome_columns") or []}
    roles = {}
    for name in schema["tables"]:
        key = str(name).lower()
        if key == label:
            roles[name] = "label"
        elif key in static:
            roles[name] = "static"
        elif key in outcome:
            roles[name] = "outcome"
        else:
            roles[name] = "temporal"
    return roles


def plan_commits(
    roles: dict[str, str],
    event_times: dict[str, list[datetime]],
    load_times: dict[str, list[datetime]],
) -> list[CommitPlan]:
    """The commits of a case's warehouse, in the order they are written.

    Static tables: one commit a day before the earliest event time in the case. Label and outcome
    tables: one commit a day after the latest event time (labels are committed at the end, as in
    bench/leakage/world.py). Every other table: its rows sorted by load time, one commit per
    distinct load time, stamped with it. `event_times` covers the non-static tables, `load_times`
    the temporal ones.
    """
    every = [t for times in event_times.values() for t in times]
    if not every:
        raise BuildError("no event time to place the commits on")
    first, last = min(every), max(every)
    plans: list[CommitPlan] = [
        CommitPlan(table, first - DAY, None) for table, role in roles.items() if role == "static"
    ]
    timed = []
    for table, role in roles.items():
        if role != "temporal":
            continue
        groups: dict[datetime, list[int]] = {}
        for index, moment in enumerate(load_times[table]):
            groups.setdefault(moment, []).append(index)
        timed += [CommitPlan(table, moment, tuple(groups[moment])) for moment in sorted(groups)]
    plans += sorted(timed, key=lambda plan: plan.logical_ts)
    plans += [
        CommitPlan(table, last + DAY, None)
        for table, role in roles.items()
        if role in ("label", "outcome")
    ]
    return plans


# --- warehouse -----------------------------------------------------------------------------------


@dataclass
class BuildResult:
    status: str  # "built" or "static_only"
    reason: str = ""
    rows: dict[str, int] = field(default_factory=dict)
    commits: dict[str, int] = field(default_factory=dict)


def read_tables(schema: dict, data_dir: Path) -> dict[str, pa.Table]:
    """The generator's CSVs as Arrow tables, read with the schema.yaml types (as check_cases.py)."""
    tables = {}
    for name, table in schema["tables"].items():
        path = data_dir / f"{name}.csv"
        if not path.exists():
            raise BuildError(f"the generator did not write {path.name}")
        columns = table["columns"]
        spec = "{" + ", ".join(f"'{col}': '{typ}'" for col, typ in columns.items()) + "}"
        quoted = str(path).replace("'", "''")
        con = duckdb.connect()
        try:
            tables[name] = con.sql(
                f"SELECT * FROM read_csv('{quoted}', header=true, columns={spec}, nullstr='', "
                "dateformat='%Y-%m-%d', timestampformat='%Y-%m-%d %H:%M:%S')"
            ).to_arrow_table()
        except duckdb.Error as err:
            raise BuildError(f"cannot read {path.name}: {err}") from None
        finally:
            con.close()
    return tables


def column_times(name: str, table: dict, column: str, data: pa.Table) -> list[datetime]:
    """The values of a time column of `data` as naive UTC datetimes (schema.yaml gives the unit)."""
    if column not in table["columns"]:
        raise BuildError(f"{name}: time column {column!r} is not in the schema")
    sql_type, unit = table["columns"][column], table.get("time_unit")
    return [to_datetime(v, sql_type, unit) for v in data.column(column).to_pylist()]


def build_warehouse(schema: dict, spec: dict, data_dir: Path, warehouse: Path) -> BuildResult:
    """Write the case's rows to an Iceberg warehouse as commits in time order (the registered
    rules). A case that cannot be placed in time is returned as static-only; nothing is written."""
    try:
        arrow_schemas = {
            name: pa.schema([(col, arrow_type(typ)) for col, typ in table["columns"].items()])
            for name, table in schema["tables"].items()
        }
        data = {
            name: table.cast(arrow_schemas[name])
            for name, table in read_tables(schema, data_dir).items()
        }
        roles = table_roles(schema, spec)
        event_times: dict[str, list[datetime]] = {}
        load_times: dict[str, list[datetime]] = {}
        for name, table in schema["tables"].items():
            if roles[name] == "static":
                continue
            event = table.get("event_time")
            if not event:
                raise BuildError(f"{name}: no event_time")
            event_times[name] = column_times(name, table, event, data[name])
            if roles[name] == "temporal":
                load = table.get("availability_time") or event
                load_times[name] = (
                    event_times[name]
                    if load == event
                    else column_times(name, table, load, data[name])
                )
        plans = plan_commits(roles, event_times, load_times)
    except BuildError as err:
        return BuildResult("static_only", str(err))

    result = BuildResult("built", rows={n: t.num_rows for n, t in data.items()})
    result.commits = {name: 0 for name in data}
    try:
        catalog = LakehouseCatalog.local(warehouse)
        for name, arrow in arrow_schemas.items():
            catalog.create_empty(name, arrow)
        for plan in plans:
            table = data[plan.table]
            if plan.rows is not None:
                table = table.take(pa.array(plan.rows, pa.int64()))
            if table.num_rows == 0:
                continue
            catalog.table(plan.table).append(
                table, snapshot_properties={LOGICAL_TS_PROPERTY: plan.logical_ts.isoformat()}
            )
            result.commits[plan.table] += 1
    except Exception as err:  # noqa: BLE001 - recorded in the report, the case runs static-only
        return BuildResult("static_only", f"warehouse write failed: {type(err).__name__}: {err}")
    return result


def generate_data(case_dir: Path, out_dir: Path) -> None:
    run = subprocess.run(
        [sys.executable, str(case_dir / "generate.py"), str(out_dir)],
        capture_output=True,
        text=True,
        check=False,
    )
    if run.returncode != 0:
        last = (run.stderr.strip().splitlines() or ["no output"])[-1]
        raise BuildError(f"generate.py failed: {last}")


# --- running the checker -------------------------------------------------------------------------


@dataclass(frozen=True)
class ModeResult:
    cls: str  # one of CLASSES
    score: float | None = None
    leak_types: tuple[str, ...] = ()
    replay_status: str | None = None
    rows_compared: int = 0
    rows_mismatched: int = 0
    detail: str = ""  # why unverifiable / the error summary / why static-only


_FRAME = re.compile(r'File "(?P<path>[^"]+)", line (?P<line>\d+), in (?P<fn>\S+)')


def error_summary(stderr: str, stdout: str = "", returncode: int | None = None) -> str:
    """The last line the CLI printed to stderr, with the deepest sqlguard frame of a traceback."""
    lines = [line for line in stderr.splitlines() if line.strip()]
    if lines:
        last = lines[-1].strip()
    elif stdout.strip():
        last = stdout.strip().splitlines()[-1].strip()
    else:
        last = f"exit code {returncode} with no output"
    frames = [m for line in lines if (m := _FRAME.search(line)) and "sqlguard" in m["path"]]
    where = (
        f" [{Path(frames[-1]['path']).name}:{frames[-1]['line']} {frames[-1]['fn']}]"
        if frames
        else ""
    )
    return last[:400] + where


def parse_cli_output(stdout: str, stderr: str, returncode: int | None) -> ModeResult:
    """One `sqlguard leakage check --json` run for one file, as an outcome."""
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        data = None
    entry = data[0] if isinstance(data, list) and len(data) == 1 else None
    if not isinstance(entry, dict) or entry.get("verdict") not in (
        "leaky",
        "clean",
        "unverifiable",
    ):
        return ModeResult("error", detail=error_summary(stderr, stdout, returncode))
    replay = entry.get("replay") or {}
    detail = ""
    if entry["verdict"] == "unverifiable":
        unchecked = [f for f in entry.get("findings", []) if f.get("rule_id") in UNCHECKED]
        detail = (
            f"{unchecked[0]['rule_id']}: {unchecked[0]['message']}"
            if unchecked
            else str(replay.get("reason") or "")
        )
    return ModeResult(
        entry["verdict"],
        float(entry["score"]),
        tuple(entry.get("leak_types") or ()),
        replay.get("status"),
        int(replay.get("rows_compared") or 0),
        int(replay.get("rows_mismatched") or 0),
        detail[:400],
    )


def run_check(
    sql: Path, spec: Path, schema: Path, warehouse: Path | None, timeout: int
) -> ModeResult:
    """`sqlguard leakage check` on one file, statically or (with a warehouse) with replay."""
    command = [
        sys.executable,
        "-m",
        "sqlguard.cli",
        "leakage",
        "check",
        str(sql),
        "--spec",
        str(spec),
        "--schema",
        str(schema),
        "--json",
    ]
    if warehouse is not None:
        command += ["--warehouse", str(warehouse), "--replay"]
    try:
        run = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout, check=False, cwd=ROOT
        )
    except subprocess.TimeoutExpired:
        return ModeResult("error", detail=f"timeout after {timeout} s")
    return parse_cli_output(run.stdout, run.stderr, run.returncode)


# --- cases ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Query:
    case: str
    file: str
    label: str  # "leaky" | "clean"
    leak_type: str | None
    also_matches: tuple[str, ...]
    pair: int
    disputed: bool

    @property
    def is_leaky(self) -> bool:
        return self.label == "leaky"


@dataclass
class Record:
    query: Query
    static: ModeResult
    replay: ModeResult

    def mode(self, rule: str) -> ModeResult:
        return self.static if rule.endswith(":static") else self.replay


def load_queries(case_dir: Path, disputed: Iterable[str]) -> list[Query]:
    meta = yaml.safe_load((case_dir / "case.yaml").read_text(encoding="utf-8"))
    return [
        Query(
            case_dir.name,
            q["file"],
            q["label"],
            q.get("leak_type"),
            tuple(q.get("also_matches") or ()),
            int(q["pair"]),
            case_dir.name in set(disputed),
        )
        for q in meta["queries"]
    ]


def cases_digest(root: Path = CASES_DIR) -> tuple[str, int]:
    """sha256 over the lines `<path>\\t<sha256 of file>` of every file under `root`, by path."""
    lines = [
        f"{p.relative_to(root).as_posix()}\t{hashlib.sha256(p.read_bytes()).hexdigest()}\n"
        for p in sorted(root.rglob("*"))
        if p.is_file() and "__pycache__" not in p.parts
    ]
    return hashlib.sha256("".join(lines).encode()).hexdigest(), len(lines)


# --- scoring -------------------------------------------------------------------------------------


def replay_score(mode: ModeResult) -> float | None:
    """The replay-only score of bench/leakage: the mismatch rate on a mismatch, else 0."""
    if mode.cls not in ("leaky", "clean", "unverifiable"):
        return None
    if mode.replay_status == "mismatch" and mode.rows_compared:
        return mode.rows_mismatched / mode.rows_compared
    return 0.0


def flagged(record: Record, rule: str, thresholds: dict[str, float], strict: bool = False) -> bool:
    """Is the query detected under `rule`? With `strict`, an unverifiable, errored or static-only
    run is a miss even when its score reaches the threshold."""
    mode = record.mode(rule)
    if strict and mode.cls not in DECIDED:
        return False
    kind, name = rule.split(":")
    if kind == "verdict":
        return mode.cls == "leaky"
    score = replay_score(mode) if name == "replay" else mode.score
    return score is not None and score >= thresholds[name]


def blocked(record: Record, rule: str, thresholds: dict[str, float]) -> bool:
    """Detected, or not decided (the product fails closed on unverifiable, error, static-only)."""
    return flagged(record, rule, thresholds) or record.mode(rule).cls not in DECIDED


def _rate(k: int, n: int) -> dict:
    return {"k": k, "n": n, "rate": round(k / n, 4) if n else None}


def _clean_ci(ci: dict) -> dict:
    return {key: [None if math.isnan(v) else v for v in interval] for key, interval in ci.items()}


def rule_metrics(
    records: Sequence[Record],
    rule: str,
    thresholds: dict[str, float],
    *,
    seed: int,
    resamples: int,
    level: float,
) -> dict:
    leaky = [r for r in records if r.query.is_leaky]
    clean = [r for r in records if not r.query.is_leaky]

    def hits(group: list[Record], strict: bool = False) -> int:
        return sum(flagged(r, rule, thresholds, strict) for r in group)

    def decided(group: list[Record]) -> list[Record]:
        return [r for r in group if r.mode(rule).cls in DECIDED]

    by_type = {}
    for leak_type in sorted({r.query.leak_type for r in leaky if r.query.leak_type}):
        group = [r for r in leaky if r.query.leak_type == leak_type]
        by_type[leak_type] = {
            "as_scored": _rate(hits(group), len(group)),
            "strict": _rate(hits(group, True), len(group)),
        }
    found = [r for r in leaky if flagged(r, rule, thresholds)]
    attribution = {
        "detected": len(found),
        "primary_type": sum(r.query.leak_type in r.mode(rule).leak_types for r in found),
        "primary_or_also_matches": sum(
            bool({r.query.leak_type, *r.query.also_matches} & set(r.mode(rule).leak_types))
            for r in found
        ),
    }
    labels = [r.query.is_leaky for r in records]
    groups = [r.query.case for r in records]
    options = {"n_resamples": resamples, "seed": seed, "level": level}
    scored = [float(flagged(r, rule, thresholds)) for r in records]
    strict_or_blocked = [
        float(
            flagged(r, rule, thresholds, True) if r.query.is_leaky else blocked(r, rule, thresholds)
        )
        for r in records
    ]
    ci = _clean_ci(bootstrap_ci(labels, scored, 1.0, groups, **options))
    ci_strict = _clean_ci(bootstrap_ci(labels, strict_or_blocked, 1.0, groups, **options))
    return {
        "n_leaky": len(leaky),
        "n_clean": len(clean),
        "recall": _rate(hits(leaky), len(leaky)),
        "fpr": _rate(hits(clean), len(clean)),
        "recall_ci95": ci["recall"],
        "fpr_ci95": ci["fpr"],
        "recall_strict": _rate(hits(leaky, True), len(leaky)),
        "block_rate_clean": _rate(sum(blocked(r, rule, thresholds) for r in clean), len(clean)),
        "recall_strict_ci95": ci_strict["recall"],
        "block_rate_clean_ci95": ci_strict["fpr"],
        "recall_decided": _rate(hits(decided(leaky)), len(decided(leaky))),
        "fpr_decided": _rate(hits(decided(clean)), len(decided(clean))),
        "by_leak_type": by_type,
        "attribution": attribution,
    }


def class_counts(records: Sequence[Record]) -> dict:
    """`{mode: {label: {class: n}}}`, every class listed."""
    counts: dict = {m: {"leaky": Counter(), "clean": Counter()} for m in MODES}
    for r in records:
        counts["static"][r.query.label][r.static.cls] += 1
        counts["replay"][r.query.label][r.replay.cls] += 1
    return {
        m: {label: {c: counts[m][label][c] for c in CLASSES} for label in ("leaky", "clean")}
        for m in MODES
    }


def per_case(records: Sequence[Record], thresholds: dict[str, float]) -> dict:
    out: dict = {}
    for case in sorted({r.query.case for r in records}):
        group = [r for r in records if r.query.case == case]
        out[case] = {
            "disputed": group[0].query.disputed,
            "outcomes": class_counts(group),
            "flagged": {
                rule: {
                    "leaky": _rate(
                        sum(flagged(r, rule, thresholds) for r in group if r.query.is_leaky),
                        sum(r.query.is_leaky for r in group),
                    ),
                    "clean": _rate(
                        sum(flagged(r, rule, thresholds) for r in group if not r.query.is_leaky),
                        sum(not r.query.is_leaky for r in group),
                    ),
                }
                for rule in RULES
            },
        }
    return out


def problem_groups(records: Sequence[Record]) -> list[dict]:
    """Errors, unverifiable and static-only runs grouped by mode, class and message."""
    groups: dict[tuple[str, str, str], list[Record]] = {}
    for r in records:
        for mode in MODES:
            result = r.static if mode == "static" else r.replay
            if result.cls in ("error", "unverifiable", "static_only"):
                groups.setdefault((mode, result.cls, result.detail), []).append(r)
    return [
        {
            "mode": mode,
            "class": cls,
            "message": detail,
            "queries": len(rs),
            "leaky": sum(r.query.is_leaky for r in rs),
            "clean": sum(not r.query.is_leaky for r in rs),
            "cases": sorted({r.query.case for r in rs}),
        }
        for (mode, cls, detail), rs in sorted(groups.items())
    ]


def summarize(
    records: Sequence[Record], thresholds: dict[str, float], seed: int, resamples: int, level: float
) -> dict:
    return {
        "cases": sorted({r.query.case for r in records}),
        "queries": len(records),
        "outcomes": class_counts(records),
        "rules": {
            rule: rule_metrics(
                records, rule, thresholds, seed=seed, resamples=resamples, level=level
            )
            for rule in RULES
        },
    }


# --- pre-registration and freeze -----------------------------------------------------------------


def git(*args: str) -> str:
    run = subprocess.run(
        ["git", "-C", str(ROOT), *args], capture_output=True, text=True, check=False
    )
    if run.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {run.stderr.strip()}")
    return run.stdout.strip()


def preflight(prereg: dict) -> list[str]:
    """Everything the registration requires to still hold; an empty list means the run may start."""
    problems = []
    try:
        dirty = git(
            "status",
            "--porcelain",
            "--",
            "bench/kaggle/eval-prereg.yaml",
            "bench/kaggle/cases",
            "src",
        )
        if dirty:
            problems.append(f"registration, cases or src are not committed as they are:\n{dirty}")
        checker = prereg["checker"]
        tree = git("rev-parse", "HEAD:src/sqlguard")
        if tree != checker["src_tree"]:
            problems.append(f"src/sqlguard tree {tree} is not the registered {checker['src_tree']}")
        release = git("rev-parse", f"{checker['release_tag']}:src/sqlguard")
        if release != checker["src_tree"]:
            problems.append(f"src/sqlguard of {checker['release_tag']} is {release}")
        frozen = subprocess.run(
            [
                "git",
                "-C",
                str(ROOT),
                "diff",
                "--quiet",
                "rules-frozen-v3",
                "HEAD",
                "--",
                "src/sqlguard/leakage",
                "src/sqlguard/core",
                "src/sqlguard/dbt",
            ],  # fmt: skip
            check=False,
        )
        if frozen.returncode != 0:
            problems.append("src/sqlguard/{leakage,core,dbt} differ from rules-frozen-v3")
    except (RuntimeError, KeyError) as err:
        problems.append(f"cannot check the checker's source: {err}")
    digest, count = cases_digest()
    inputs = prereg["inputs"]
    if (digest, count) != (inputs["cases_digest"], inputs["cases_file_count"]):
        problems.append(f"cases digest {digest} ({count} files) is not the registered one")
    thr = prereg["thresholds"]
    source = ROOT / thr["source"]
    if hashlib.sha256(source.read_bytes()).hexdigest() != thr["source_sha256"]:
        problems.append(f"{thr['source']} changed since the registration")
    else:
        frozen_thresholds = json.loads(source.read_text(encoding="utf-8"))["thresholds"]
        for name in ("static", "replay", "combined"):
            if frozen_thresholds[name] != thr[name]:
                problems.append(
                    f"threshold {name}: registered {thr[name]}, source {frozen_thresholds[name]}"
                )
    return problems


def environment() -> dict:
    import pyiceberg
    import sqlglot

    lock = ROOT / "uv.lock"
    return {
        "python": sys.version.split()[0],
        "sqlglot": sqlglot.__version__,
        "duckdb": duckdb.__version__,
        "pyiceberg": pyiceberg.__version__,
        "uv_lock_sha256": hashlib.sha256(lock.read_bytes()).hexdigest() if lock.exists() else None,
    }


# --- output --------------------------------------------------------------------------------------

CSV_COLUMNS = (
    "case", "file", "label", "leak_type", "also_matches", "disputed", "mode", "class", "score",
    "leak_types", "replay_status", "rows_compared", "rows_mismatched", "detail",
)  # fmt: skip


def csv_rows(records: Sequence[Record]) -> list[dict]:
    rows = []
    for r in records:
        for mode in MODES:
            m = r.static if mode == "static" else r.replay
            rows.append(
                {
                    "case": r.query.case,
                    "file": r.query.file,
                    "label": r.query.label,
                    "leak_type": r.query.leak_type or "",
                    "also_matches": ",".join(r.query.also_matches),
                    "disputed": int(r.query.disputed),
                    "mode": mode,
                    "class": m.cls,
                    "score": "" if m.score is None else f"{m.score:.4f}",
                    "leak_types": ",".join(m.leak_types),
                    "replay_status": m.replay_status or "",
                    "rows_compared": m.rows_compared,
                    "rows_mismatched": m.rows_mismatched,
                    "detail": m.detail,
                }
            )
    return rows


def _cell(rate: dict, ci: Sequence | None = None) -> str:
    if rate["rate"] is None:
        return f"{rate['k']}/{rate['n']}"
    text = f"{rate['k']}/{rate['n']} = {rate['rate']:.2f}"
    if ci and ci[0] is not None:
        text += f" [{ci[0]:.2f}, {ci[1]:.2f}]"
    return text


def _headline(summary: dict) -> list[str]:
    lines = [
        "| rule | recall [95% CI] | FPR [95% CI] | recall, unverifiable/error as misses |"
        " clean blocked (flagged, unverifiable or error) | recall, decided only |"
        " FPR, decided only |",
        "|---|---|---|---|---|---|---|",
    ]
    for rule, m in summary["rules"].items():
        lines.append(
            f"| {rule} | {_cell(m['recall'], m['recall_ci95'])} | {_cell(m['fpr'], m['fpr_ci95'])}"
            f" | {_cell(m['recall_strict'], m['recall_strict_ci95'])}"
            f" | {_cell(m['block_rate_clean'], m['block_rate_clean_ci95'])}"
            f" | {_cell(m['recall_decided'])} | {_cell(m['fpr_decided'])} |"
        )
    return lines


def _types(summary: dict) -> list[str]:
    types = sorted({t for m in summary["rules"].values() for t in m["by_leak_type"]})
    lines = ["| rule | " + " | ".join(types) + " |", "|---|" + "---|" * len(types)]
    for rule, m in summary["rules"].items():
        cells = []
        for t in types:
            entry = m["by_leak_type"].get(t)
            if entry is None:
                cells.append("-")
                continue
            scored, strict = entry["as_scored"], entry["strict"]
            note = "" if scored["k"] == strict["k"] else f" (strict {strict['k']})"
            cells.append(f"{scored['k']}/{scored['n']}{note}")
        lines.append(f"| {rule} | " + " | ".join(cells) + " |")
    return lines


def _letters(entry: dict, mode: str) -> str:
    counts: Counter = Counter()
    for label in ("leaky", "clean"):
        counts.update(entry["outcomes"][mode][label])
    return " ".join(f"{k[0].upper()}{counts[k]}" for k in CLASSES if counts[k])


def render_markdown(result: dict) -> str:
    meta, thr = result["meta"], result["meta"]["thresholds"]
    out = [
        f"# Kaggle leak cases: frozen checker {meta['checker']['release_tag']}",
        "",
        f"Run `{meta['stem']}` of `bench/kaggle/evaluate.py` at {meta['git_sha']}"
        f"{' (dirty tree)' if meta['git_dirty'] else ''}. Registration:"
        f" `bench/kaggle/eval-prereg.yaml` (commit {meta['prereg_commit']}). Cases digest"
        f" `{meta['cases_digest'][:16]}`, checker"
        f" `src/sqlguard` tree `{meta['checker']['src_tree'][:12]}`.",
        "",
        f"Thresholds (frozen): static {thr['static']}, replay {thr['replay']:.6f}, combined"
        f" {thr['combined']}; verdict threshold {meta['verdict_threshold']}. Intervals: 95% cluster"
        f" bootstrap by case, {meta['resamples']} resamples, seed {meta['seed']}. Replay runs use"
        " the CLI defaults (24 prediction times, sampling seed 0).",
        "",
        "Rules: `verdict:*` counts a query detected when the checker says leaky (static run, or"
        " the run with replay); `threshold:*` applies the frozen thresholds to the static score,"
        " the replay mismatch rate and the combined score. A query that errored has no score and"
        ' is not detected. An unverifiable query keeps the checker\'s score. "As misses" counts'
        " unverifiable, error and static-only runs as not detected even when their score reaches"
        " the threshold.",
        "",
        "## Outcomes of the 160 runs",
        "",
        "| mode | label | " + " | ".join(CLASSES) + " |",
        "|---|---|" + "---|" * len(CLASSES),
    ]
    for mode in MODES:
        for label in ("leaky", "clean"):
            counts = result["all"]["outcomes"][mode][label]
            out.append(
                f"| {mode} | {label} | " + " | ".join(str(counts[c]) for c in CLASSES) + " |"
            )
    for key, title in (("all", "All 14 cases"), ("without_disputed", "Without kl05, kl09, kl14")):
        summary = result[key]
        out += [
            "",
            f"## {title} ({summary['queries']} queries)",
            "",
            *_headline(summary),
            "",
            "Detected leaky queries by primary leak type (as scored; strict where it differs):",
            "",
            *_types(summary),
        ]
    out += [
        "",
        "## Per case",
        "",
        "Flagged leaky/clean queries per rule, and the outcome classes of the leaky+clean queries"
        " (L leaky, C clean, U unverifiable, E error, S static-only).",
        "",
        "| case | disputed | warehouse | static runs | replay runs | " + " | ".join(RULES) + " |",
        "|---|---|---|---|---|" + "---|" * len(RULES),
    ]
    for case, entry in result["per_case"].items():
        build = result["build"][case]
        out.append(
            f"| {case} | {'yes' if entry['disputed'] else ''} | {build['status']} |"
            f" {_letters(entry, 'static')} | {_letters(entry, 'replay')} | "
            + " | ".join(
                f"{entry['flagged'][r]['leaky']['k']}/{entry['flagged'][r]['leaky']['n']}"
                f" - {entry['flagged'][r]['clean']['k']}/{entry['flagged'][r]['clean']['n']}"
                for r in RULES
            )
            + " |"
        )
    out += ["", "## Errors, unverifiable and static-only runs", ""]
    if result["problems"]:
        out += [
            "| mode | class | queries (leaky/clean) | cases | message |",
            "|---|---|---|---|---|",
        ]
        for p in result["problems"]:
            message = p["message"].replace("|", "\\|").replace("\n", " ")
            out.append(
                f"| {p['mode']} | {p['class']} | {p['queries']} ({p['leaky']}/{p['clean']}) |"
                f" {', '.join(p['cases'])} | {message} |"
            )
    else:
        out.append("None.")
    out += [
        "",
        "## Warehouses",
        "",
        "| case | status | rows | commits | reason |",
        "|---|---|---|---|---|",
    ]
    for case, build in result["build"].items():
        rows = ", ".join(f"{t}={n}" for t, n in build["rows"].items())
        commits = ", ".join(f"{t}={n}" for t, n in build["commits"].items())
        out.append(f"| {case} | {build['status']} | {rows} | {commits} | {build['reason']} |")
    attribution = result["all"]["rules"]
    out += [
        "",
        "## Type attribution of detected leaky queries",
        "",
        "| rule | detected | names the primary type | names the primary type or an also_matches |",
        "|---|---|---|---|",
    ]
    for rule, m in attribution.items():
        a = m["attribution"]
        out.append(
            f"| {rule} | {a['detected']} | {a['primary_type']} | {a['primary_or_also_matches']} |"
        )
    return "\n".join(out) + "\n"


# --- main ----------------------------------------------------------------------------------------


def run(args: argparse.Namespace) -> int:
    prereg = yaml.safe_load(PREREG_PATH.read_text(encoding="utf-8"))
    if problems := preflight(prereg):
        print("refusing to run:\n" + "\n".join(f"- {p}" for p in problems), file=sys.stderr)
        return 2
    thresholds = {k: float(prereg["thresholds"][k]) for k in ("static", "replay", "combined")}
    interval = prereg["intervals"]
    disputed = prereg["disputed_labels"]["cases"]
    case_ids = list(prereg["inputs"]["cases"])
    queries = [q for cid in case_ids for q in load_queries(CASES_DIR / cid, disputed)]
    if len(queries) != prereg["inputs"]["queries"]["total"]:
        print(f"refusing to run: {len(queries)} queries, not the registered count", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory(prefix="sqlguard-kaggle-", dir=args.work_dir) as tmp:
        work = Path(tmp)
        build: dict[str, BuildResult] = {}
        schema_files: dict[str, Path] = {}
        for cid in case_ids:
            case_dir = CASES_DIR / cid
            schema = yaml.safe_load((case_dir / "schema.yaml").read_text(encoding="utf-8"))
            spec = yaml.safe_load((case_dir / "spec.yaml").read_text(encoding="utf-8"))
            schema_files[cid] = work / cid / "schema-checker.yaml"
            schema_files[cid].parent.mkdir(parents=True)
            schema_files[cid].write_text(
                yaml.safe_dump(convert_schema(schema), sort_keys=False), encoding="utf-8"
            )
            try:
                data_dir = work / cid / "data"
                data_dir.mkdir()
                generate_data(case_dir, data_dir)
                build[cid] = build_warehouse(schema, spec, data_dir, work / cid / "warehouse")
            except BuildError as err:
                build[cid] = BuildResult("static_only", str(err))
            print(f"{cid}: warehouse {build[cid].status} {build[cid].reason}".rstrip(), flush=True)
        if args.dry_run:
            return 0

        def check(task: tuple[Query, str]) -> ModeResult:
            query, mode = task
            case_dir = CASES_DIR / query.case
            if mode == "replay" and build[query.case].status != "built":
                return ModeResult("static_only", detail=build[query.case].reason)
            warehouse = work / query.case / "warehouse" if mode == "replay" else None
            return run_check(
                case_dir / query.file,
                case_dir / "spec.yaml",
                schema_files[query.case],
                warehouse,
                int(prereg["runs"]["timeout_seconds"]),
            )

        tasks = [(q, m) for q in queries for m in MODES]
        with ThreadPoolExecutor(max_workers=args.jobs) as pool:
            outcomes = list(pool.map(check, tasks))
        by_task = dict(zip(((q.case, q.file, m) for q, m in tasks), outcomes, strict=True))
        records = [
            Record(q, by_task[(q.case, q.file, "static")], by_task[(q.case, q.file, "replay")])
            for q in queries
        ]

    sha = git("rev-parse", "--short=7", "HEAD")
    stem = f"{datetime.now(UTC):%Y%m%d}-{sha}"
    seed, resamples, level = (
        int(interval["seed"]),
        int(interval["resamples"]),
        float(interval["level"]),
    )
    result = {
        "meta": {
            "stem": stem,
            "git_sha": sha,
            "git_dirty": bool(git("status", "--porcelain", "--", ".", ":!results/kaggle")),
            "prereg_commit": git(
                "log", "--diff-filter=A", "--format=%h", "--", "bench/kaggle/eval-prereg.yaml"
            ),
            "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "checker": prereg["checker"],
            "cases_digest": cases_digest()[0],
            "thresholds": thresholds,
            "verdict_threshold": prereg["checker"]["verdict_threshold"],
            "seed": seed,
            "resamples": resamples,
            "level": level,
            "disputed": disputed,
            "environment": environment(),
        },
        "build": {cid: asdict(b) for cid, b in build.items()},
        "all": summarize(records, thresholds, seed, resamples, level),
        "without_disputed": summarize(
            [r for r in records if not r.query.disputed], thresholds, seed, resamples, level
        ),
        "per_case": per_case(records, thresholds),
        "problems": problem_groups(records),
        "queries": csv_rows(records),
    }
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{stem}.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    with (out / f"{stem}.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(csv_rows(records))
    (out / f"{stem}.md").write_text(render_markdown(result), encoding="utf-8")
    print(f"wrote {out / stem}.json, .csv and .md")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--jobs", type=int, default=4, help="checker processes at a time")
    parser.add_argument("--out-dir", default=str(RESULTS_DIR))
    parser.add_argument(
        "--work-dir", help="parent of the temporary warehouses (default: system temp)"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="build the warehouses only; the checker is not run"
    )
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
