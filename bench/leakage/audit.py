"""Mechanical label audit of a template set, from what the data generator knows.

An author labels a template `clean` or `L1`..`L7` by reasoning. This checks the leaky/clean half of
that claim by running every rendering twice and comparing:

- on the **final tables**, as a training query is normally run;
- on the tables **as they were knowable at a prediction time `t`**, built from the generator's own
  load times, one world per sampled `t`: `events` loaded before `t` (`ingested_at < t`),
  `user_stats` stamped before `t`, `plan_history` versions loaded before `t` with a `valid_to` only
  where the next version was loaded before `t` (`world.PlanVersion.as_row`), `entities` as they
  are, `user_outcomes` empty, and the label spine as it stood: rows after `t` absent, labels of
  rows more than `horizon` old kept, every other label hidden (NULL).

A feature that differs between the two for any row at `t` used something unknown at `t`: leaky, by
construction. A template whose renderings never differ at any sampled time is consistent with
`clean`. No Iceberg snapshot is read (that is replay) and no detector of the project runs; the
truth is the generator's, so the audit is independent of the system under test.

What it cannot decide is listed as **not auditable**, with the reason:

- `L6` (lineage to the label or outcomes) and `L7` (the wall clock is the same in both runs);
- any template that reads the wall clock, whatever its label;
- a time-split cutoff computed from the spine: `label_ts` compared (`<`, `<=`, `>`, `>=`, `BETWEEN`)
  with `(SELECT MAX|MIN|quantile|median(label_ts) FROM labels)` in a WHERE, ON, HAVING or QUALIFY.
  The documented rules accept it by design (a split is chosen before any prediction time exists),
  so the audit keeps the author's label;
- a rendering that does not parse or run on the world, has no `user_id`/`label_ts` or feature
  column in its output, or returns rows at fewer than `MIN_POINTS` sampled times (for `clean`).

It checks leaky versus clean, not the type: whether a leak is `L2` or `L3` stays the author's call.

Two conventions are stricter than they may look, and are stated to the authors: the spine after `t`
does not exist at `t` (a value *read* from later spine rows, such as `MAX(label_ts)` in the select
list, depends on the future; a cutoff that only chooses training rows is exempt, above), and a
label is final only when `label_ts + horizon < t` (strictly).

Policy: a disagreement goes back to the author. Labels are never changed here.

Usage: python -m bench.leakage.audit --candidate holdout_v2 | --candidate-dir DIR [--points N]
"""

from __future__ import annotations

import argparse
import json
import math
import random
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import yaml
from sqlglot import exp, parse_one

from bench.leakage import world as gen
from bench.leakage.baseline import bind_relation_map
from bench.leakage.cases import TEMPLATES_DIR, LeakCase, Template, render
from bench.leakage.duplicates import load_directory
from bench.leakage.splits import load_splits
from sqlguard.core.duck import connect

MICROSECOND = timedelta(microseconds=1)
PASSTHROUGH = ("user_id", "label_ts", "churned")
AUDITABLE = ("clean", "L1", "L2", "L3", "L4", "L5")
NOT_AUDITABLE_TYPES = {
    "L6": "L6 is judged by lineage to the label and outcomes, which this audit does not model",
    "L7": "L7: the wall clock reads the same in both runs, so a difference cannot show",
}
DEFAULT_POINTS = 0  # every prediction time: a rare leak is found only if its time is audited
MIN_POINTS = 3  # sampled times with output needed before a rendering can be called clean
_CLOCK = re.compile(
    r"\b(now|today|current_date|current_time|current_timestamp|localtimestamp|localtime|"
    r"get_current_timestamp|get_current_time|transaction_timestamp|statement_timestamp|"
    r"clock_timestamp|current_localtimestamp|current_localtime)\b"
)


@dataclass
class CaseResult:
    """What the audit found for one rendering. `verdict` is leaky, clean or unauditable."""

    case_id: str
    verdict: str
    reason: str = ""
    points_with_rows: int = 0
    evidence: dict = field(default_factory=dict)


@dataclass
class TemplateResult:
    template_id: str
    label: str
    status: str  # agree | disagree | not_auditable
    reason: str
    renderings: dict[str, int]  # verdict -> count
    evidence: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


# --- the world as it was knowable --------------------------------------------------------------


class AuditWorld:
    """The final tables plus the generator's load times, and the tables as of any time `t`."""

    def __init__(self, tables: dict[str, pa.Table], versions: list[gen.PlanVersion]) -> None:
        self.final = tables
        self.versions = versions
        self.horizon = timedelta(days=gen.WorldConfig().horizon_days)

    @classmethod
    def from_catalog(cls, catalog) -> AuditWorld:
        tables = {name: catalog.read(name) for name in gen.SCHEMAS}
        return cls(tables, gen.plan_versions(catalog))

    def prediction_times(self) -> list[datetime]:
        return sorted(set(self.final["labels"].column("label_ts").to_pylist()))

    def known_at(self, t: datetime) -> dict[str, pa.Table]:
        """Every table as the model could have read it at prediction time `t` (strict)."""
        cut = pa.scalar(t, pa.timestamp("us"))

        def before(table: pa.Table, column: str) -> pa.Table:
            return table.filter(pc.less(table.column(column), cut))

        now = t - MICROSECOND
        plans = [v.as_row(now) for v in self.versions if v.ingested_at <= now]
        spine = []
        for row in self.final["labels"].to_pylist():
            if row["label_ts"] > t:
                continue
            final = row["label_ts"] + self.horizon < t
            spine.append({**row, "churned": row["churned"] if final else None})
        return {
            "entities": self.final["entities"],
            "events": before(self.final["events"], "ingested_at"),
            "user_stats": before(self.final["user_stats"], "updated_at"),
            "plan_history": pa.Table.from_pylist(plans, schema=gen.SCHEMAS["plan_history"]),
            "user_outcomes": gen.SCHEMAS["user_outcomes"].empty_table(),
            "labels": pa.Table.from_pylist(spine, schema=gen.SCHEMAS["labels"]),
        }

    def sample_points(self, k: int, seed: int) -> list[datetime]:
        """Prediction times to audit: where most is in flight, the busiest, midnights, then random.

        `k <= 0` means every distinct prediction time.
        """
        times = self.prediction_times()
        if k <= 0 or k >= len(times):
            return times
        spine = self.final["labels"].column("label_ts").to_pylist()
        rows = {t: spine.count(t) for t in times}
        events = self.final["events"]
        event_ts = events.column("event_ts").to_pylist()
        ingested = events.column("ingested_at").to_pylist()
        in_flight = {
            t: sum(e < t <= i for e, i in zip(event_ts, ingested, strict=True))
            + sum(v.valid_from < t <= v.ingested_at for v in self.versions)
            for t in times
        }
        rng = random.Random(seed)
        chosen: list[datetime] = []

        def take(candidates: Iterable[datetime], n: int) -> None:
            for t in candidates:
                if len(chosen) >= k or n <= 0:
                    return
                if t not in chosen:
                    chosen.append(t)
                    n -= 1

        third = max(1, k // 3)
        take(sorted(times, key=lambda t: (-in_flight[t], t)), third)
        take(sorted(times, key=lambda t: (-rows[t], t)), third)
        midnights = [t for t in times if t == t.replace(hour=0, minute=0, second=0, microsecond=0)]
        rng.shuffle(midnights)
        take(midnights, max(1, k // 4))
        rest = [t for t in times if t not in chosen]
        rng.shuffle(rest)
        take(rest, k - len(chosen))
        return sorted(chosen)


# --- running a rendering on a world -------------------------------------------------------------


def _norm(value: object) -> object:
    if isinstance(value, Decimal):
        return float(value)
    return value


def _sort_key(row: tuple) -> tuple:
    """Order-free and blind to float noise: floats are keyed rounded, compared exactly later."""
    return tuple(
        ("f", round(v, 3)) if isinstance(v, float) and v == v else ("o", repr(v)) for v in row
    )


def _same(a: object, b: object) -> bool:
    if isinstance(a, float) and isinstance(b, float):
        return (a != a and b != b) or math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-6)
    return a == b


def same_rows(want: list[tuple], got: list[tuple]) -> bool:
    return len(want) == len(got) and all(
        len(x) == len(y) and all(_same(u, v) for u, v in zip(x, y, strict=True))
        for x, y in zip(want, got, strict=True)
    )


def _features(columns: list[str], rows: list[tuple], t: datetime) -> list[tuple] | None:
    """Feature values of the rows at prediction time `t`, order-free; None without the columns."""
    if "user_id" not in columns or "label_ts" not in columns:
        return None
    ts, keep = columns.index("label_ts"), [i for i, c in enumerate(columns) if c not in PASSTHROUGH]
    picked = [
        (row[columns.index("user_id")], *(_norm(row[i]) for i in keep))
        for row in rows
        if row[ts] == t
    ]
    return sorted(picked, key=_sort_key)


def _connection(tables: dict[str, pa.Table], relations: Mapping[str, str]):
    conn = connect(tables)
    bind_relation_map(conn, relations)
    return conn


def _run(conn, sql: str) -> tuple[list[str], list[tuple]]:
    relation = conn.sql(sql)
    return list(relation.columns), relation.fetchall()


SPINE_TABLE = "labels"
SPINE_TIME = "label_ts"
_CUTOFF_AGGREGATES = (
    exp.Max,
    exp.Min,
    exp.Median,
    exp.Quantile,
    exp.ApproxQuantile,
    exp.PercentileCont,
    exp.PercentileDisc,
)
_CUTOFF_COMPARISONS = (exp.LT, exp.LTE, exp.GT, exp.GTE, exp.Between)


def _is_spine_cutoff(subquery: exp.Subquery, relations: Mapping[str, str]) -> bool:
    """`(SELECT MAX|MIN|quantile|median(label_ts) FROM labels)` and nothing else in it."""
    select = subquery.this
    if not isinstance(select, exp.Select) or len(select.expressions) != 1:
        return False
    extras = {k for k, v in select.args.items() if v and k not in ("expressions", "from_")}
    source = select.args.get("from_")
    if extras or source is None or not isinstance(source.this, exp.Table):
        return False
    table = source.this
    qualified = ".".join(p.lower() for p in (table.catalog, table.db, table.name) if p)
    if (relations.get(qualified) or table.name).rsplit(".", 1)[-1].lower() != SPINE_TABLE:
        return False
    aggregate = select.expressions[0]
    if isinstance(aggregate, exp.Alias):
        aggregate = aggregate.this
    column = aggregate.find(exp.Column)
    return (
        isinstance(aggregate, _CUTOFF_AGGREGATES)
        and column is not None
        and column.name.lower() == SPINE_TIME
    )


def has_spine_cutoff(sql: str, relations: Mapping[str, str] | None = None) -> bool:
    """Whether the query filters rows by a time-split cutoff taken from the whole spine.

    The subquery has to sit in a comparison (`<`, `<=`, `>`, `>=`, `BETWEEN`) inside a `WHERE`,
    `ON`, `HAVING` or `QUALIFY`. The same subquery in the select list is a feature that reads
    the future, and `= MAX(label_ts)` is a documented leak; both stay auditable.
    """
    tree = parse_one(sql, read="duckdb")
    for node in tree.find_all(exp.Subquery):
        if not _is_spine_cutoff(node, relations or {}):
            continue
        comparison = node.find_ancestor(*_CUTOFF_COMPARISONS)
        if comparison is not None and comparison.find_ancestor(
            exp.Where, exp.Join, exp.Having, exp.Qualify
        ):
            return True
    return False


def reads_the_clock(sql: str) -> bool:
    """Whether the query calls a wall-clock function (comments and string literals ignored)."""
    text = parse_one(sql, read="duckdb").sql(dialect="duckdb").lower()
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.DOTALL)  # the comments sqlglot keeps
    text = re.sub(r"'(?:[^']|'')*'", "''", text)
    return _CLOCK.search(text) is not None


def audit_cases(
    cases: list[LeakCase],
    audit_world: AuditWorld,
    relations: Mapping[str, str],
    points: list[datetime],
) -> dict[str, CaseResult]:
    """Leaky, clean or unauditable for every rendering."""
    results: dict[str, CaseResult] = {}
    final_conn = _connection(audit_world.final, relations)
    final: dict[str, tuple[list[str], list[tuple]]] = {}
    for case in cases:
        try:
            if reads_the_clock(case.sql):
                results[case.case_id] = CaseResult(
                    case.case_id, "unauditable", "reads the wall clock (the audit cannot see L7)"
                )
                continue
            if has_spine_cutoff(case.sql, relations):
                results[case.case_id] = CaseResult(
                    case.case_id,
                    "unauditable",
                    "spine-derived split cutoff (accepted by design)",
                )
                continue
            final[case.case_id] = _run(final_conn, case.sql)
        except Exception as err:  # noqa: BLE001 - any failure to run is a finding
            results[case.case_id] = CaseResult(
                case.case_id, "unauditable", f"does not run on the world: {str(err)[:160]}"
            )
    counted: dict[str, int] = defaultdict(int)
    for t in points:
        live = [c for c in cases if c.case_id in final and results.get(c.case_id) is None]
        if not live:
            break
        conn = _connection(audit_world.known_at(t), relations)
        for case in live:
            columns, rows = final[case.case_id]
            if not [c for c in columns if c not in PASSTHROUGH]:
                results[case.case_id] = CaseResult(
                    case.case_id, "unauditable", "the output has no feature column"
                )
                continue
            want = _features(columns, rows, t)
            if want is None:
                results[case.case_id] = CaseResult(
                    case.case_id, "unauditable", "the output lacks user_id or label_ts"
                )
                continue
            try:
                got_columns, got_rows = _run(conn, case.sql)
            except Exception as err:  # noqa: BLE001
                results[case.case_id] = CaseResult(
                    case.case_id, "unauditable", f"does not run as of {t}: {str(err)[:160]}"
                )
                continue
            got = _features(got_columns, got_rows, t) or []
            if not want and not got:
                continue
            counted[case.case_id] += 1
            if not same_rows(want, got):
                results[case.case_id] = CaseResult(
                    case.case_id,
                    "leaky",
                    points_with_rows=counted[case.case_id],
                    evidence=_difference(t, want, got),
                )
    for case in cases:
        if case.case_id in results:
            continue
        n = counted[case.case_id]
        if n >= MIN_POINTS:
            results[case.case_id] = CaseResult(case.case_id, "clean", points_with_rows=n)
        else:
            results[case.case_id] = CaseResult(
                case.case_id,
                "unauditable",
                f"returns rows at only {n} of {len(points)} sampled times (needs {MIN_POINTS})",
                n,
            )
    return results


def _difference(t: datetime, want: list[tuple], got: list[tuple]) -> dict:
    """The first row that differs, for the author."""
    only_final = [row for row in want if not any(same_rows([row], [g]) for g in got)]
    only_known = [row for row in got if not any(same_rows([row], [w]) for w in want)]
    return {
        "time": t.isoformat(),
        "rows_final": len(want),
        "rows_known_at_t": len(got),
        "example_final": list(only_final[0]) if only_final else None,
        "example_known_at_t": list(only_known[0]) if only_known else None,
    }


# --- per template -------------------------------------------------------------------------------


def audit_template(template: Template, results: Mapping[str, CaseResult]) -> TemplateResult:
    cases = render(template)
    verdicts = [results[c.case_id] for c in cases]
    count: dict[str, int] = defaultdict(int)
    for r in verdicts:
        count[r.verdict] += 1
    counts = {k: count[k] for k in ("leaky", "clean", "unauditable")}
    label = template.leak_type
    if label in NOT_AUDITABLE_TYPES:
        return TemplateResult(
            template.template_id, label, "not_auditable", NOT_AUDITABLE_TYPES[label], counts
        )
    if count["unauditable"] == len(verdicts):
        reasons = sorted({r.reason for r in verdicts})
        return TemplateResult(
            template.template_id, label, "not_auditable", "; ".join(reasons)[:300], counts
        )
    leaky = next((r for r in verdicts if r.verdict == "leaky"), None)
    warnings = []
    if count["unauditable"]:
        warnings.append(f"{count['unauditable']} of {len(verdicts)} renderings not auditable")
    if label == "clean":
        if leaky is None:
            return TemplateResult(template.template_id, label, "agree", "", counts, {}, warnings)
        return TemplateResult(
            template.template_id,
            label,
            "disagree",
            f"labelled clean, but rendering {leaky.case_id} uses something unknown at t",
            counts,
            leaky.evidence,
            warnings,
        )
    if leaky is not None:
        if count["clean"]:
            warnings.append(
                f"{count['clean']} of {len(verdicts)} renderings show no leak (a template should "
                "have one label for every rendering)"
            )
        return TemplateResult(
            template.template_id, label, "agree", "", counts, leaky.evidence, warnings
        )
    return TemplateResult(
        template.template_id,
        label,
        "disagree",
        f"labelled {label}, but no rendering differs from the as-of-t truth at any sampled time",
        counts,
        {},
        warnings,
    )


def audit_templates(
    templates: list[Template],
    audit_world: AuditWorld,
    relations: Mapping[str, str],
    points: list[datetime],
) -> list[TemplateResult]:
    cases = [case for t in templates for case in render(t)]
    results = audit_cases(cases, audit_world, relations, points)
    return [audit_template(t, results) for t in templates]


def summary(results: list[TemplateResult]) -> dict:
    by_status: dict[str, int] = defaultdict(int)
    for r in results:
        by_status[r.status] += 1
    return {
        "templates": len(results),
        "agree": by_status["agree"],
        "disagree": by_status["disagree"],
        "not_auditable": by_status["not_auditable"],
    }


# --- command line -------------------------------------------------------------------------------


def _relations_of(split: str) -> dict[str, str]:
    spec = yaml.safe_load(load_splits()[split].spec.read_text(encoding="utf-8"))
    return {str(k): str(v) for k, v in (spec.get("relations") or {}).items()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m bench.leakage.audit", description=__doc__)
    where = parser.add_mutually_exclusive_group(required=True)
    where.add_argument("--candidate", help="a split of the repository, e.g. holdout_v2")
    where.add_argument("--candidate-dir", type=Path, help="a directory of template files")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--warehouse", type=Path, default=Path(".warehouse/bench"))
    parser.add_argument(
        "--points",
        type=int,
        default=DEFAULT_POINTS,
        help="prediction times to audit (default 0 = every one; a number samples that many)",
    )
    parser.add_argument(
        "--relations-of",
        default="holdout_v2",
        help="the split whose spec's `relations` name the qualified tables (default: holdout_v2)",
    )
    parser.add_argument("--out", type=Path, help="write the full result as JSON")
    args = parser.parse_args(argv)

    if args.candidate:
        if args.candidate not in load_splits():
            parser.error(f"unknown split {args.candidate!r}; known: {sorted(load_splits())}")
        templates = load_directory(TEMPLATES_DIR / args.candidate, args.candidate)
    else:
        templates = load_directory(args.candidate_dir)
    if not templates:
        print("no candidate templates found")
        return 2

    from bench.leakage.run import synthetic_world

    audit_world = AuditWorld.from_catalog(synthetic_world(args.warehouse, args.seed))
    points = audit_world.sample_points(args.points, args.seed)
    results = audit_templates(templates, audit_world, _relations_of(args.relations_of), points)

    totals = summary(results)
    print(
        f"label audit of {totals['templates']} templates at {len(points)} prediction times: "
        f"{totals['agree']} agree, {totals['disagree']} disagree, "
        f"{totals['not_auditable']} not auditable"
    )
    for r in results:
        if r.status == "disagree":
            print(f"  DISAGREE {r.template_id} ({r.label}): {r.reason}")
            if r.evidence:
                print(f"           {json.dumps(r.evidence)}")
    for r in results:
        if r.status == "not_auditable":
            print(f"  not auditable {r.template_id} ({r.label}): {r.reason}")
    for r in results:
        for warning in r.warnings:
            print(f"  warning {r.template_id}: {warning}")
    if args.out:
        payload = {
            "points": [t.isoformat() for t in points],
            "summary": totals,
            "templates": [vars(r) for r in results],
        }
        args.out.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
    return 1 if totals["disagree"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
