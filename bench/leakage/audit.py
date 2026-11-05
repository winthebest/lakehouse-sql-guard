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
- a rendering that does not parse or run on the world, has no `user_id`/`label_ts` or feature
  column in its output, or returns rows at fewer than `MIN_POINTS` sampled times (for `clean`).

It checks leaky versus clean, not the type: whether a leak is `L2` or `L3` stays the author's call.

A time-split cutoff is the one pattern the documented rules accept by design (a split is chosen
before any prediction time exists): the spine's `label_ts` compared with an uncorrelated
`(SELECT MAX|MIN|quantile|median(label_ts) FROM labels)`. The audit computes that value once from
the final spine, writes it into the query as a literal for both runs, and audits everything else
as written; `fix_spine_cutoffs` is exact about what counts: the comparison sits in the WHERE, ON,
HAVING or QUALIFY of its own nearest SELECT (never in a select list, nested or not), and the
subquery reads the spine itself, never a CTE (a CTE named `labels` included). Any other use of
such a subquery is audited as it stands.

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
from collections import Counter, defaultdict
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
    "L6": (
        "not auditable: L6 is judged by lineage to the label and outcomes, which this audit does "
        "not model; an L6 label is the author's alone"
    ),
    "L7": (
        "not auditable: L7: the wall clock reads the same in both runs, so a difference cannot show"
    ),
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
    cutoffs: list[str] = field(default_factory=list)  # split cutoffs fixed to their final value


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


def _cte_names(tree: exp.Expression) -> set[str]:
    return {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}


def _reads_spine(table: exp.Table, relations: Mapping[str, str], ctes: set[str]) -> bool:
    """Whether a table reference is the spine itself.

    A bare name that a CTE of the query defines is that CTE, whatever it is called and whatever
    it selects (`WITH labels AS (SELECT ... WHERE churned = 1)` is not the spine), and a cutoff
    read through any CTE is audited as written: a copy cannot be told from a filtered or
    rewritten relation without reading it, and a wrong guess hides a leak.
    """
    if not table.db and not table.catalog and table.name.lower() in ctes:
        return False
    qualified = ".".join(p.lower() for p in (table.catalog, table.db, table.name) if p)
    return (relations.get(qualified) or table.name).rsplit(".", 1)[-1].lower() == SPINE_TABLE


def _is_spine_cutoff(subquery: exp.Subquery, relations: Mapping[str, str], ctes: set[str]) -> bool:
    """`(SELECT MAX|MIN|quantile|median(label_ts) FROM <the spine>)` and nothing else in it."""
    select = subquery.this
    if not isinstance(select, exp.Select) or len(select.expressions) != 1:
        return False
    extras = {k for k, v in select.args.items() if v and k not in ("expressions", "from_")}
    source = select.args.get("from_")
    if extras or source is None or not isinstance(source.this, exp.Table):
        return False
    if not _reads_spine(source.this, relations, ctes):
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


def _spine_names(tree: exp.Expression, relations: Mapping[str, str], ctes: set[str]) -> set[str]:
    """Aliases and names under which the query reads the spine table."""
    return {
        table.alias_or_name.lower()
        for table in tree.find_all(exp.Table)
        if _reads_spine(table, relations, ctes)
    }


def _is_spine_time(node: exp.Expression, spine: set[str]) -> bool:
    """The spine's own `label_ts` column (a bare `label_ts` counts when the spine is read)."""
    if isinstance(node, exp.Paren):
        node = node.this
    if not isinstance(node, exp.Column) or node.name.lower() != SPINE_TIME:
        return False
    return node.table.lower() in spine if node.table else bool(spine)


def _holds(side: exp.Expression, node: exp.Expression) -> bool:
    """`side` is `node` itself, or `node` plus or minus an interval (arithmetic outside it)."""
    while isinstance(side, exp.Paren):
        side = side.this
    if side is node:
        return True
    if isinstance(side, exp.Add | exp.Sub):
        inner, other = (side.this, side.expression)
        if isinstance(inner, exp.Interval):
            inner, other = other, inner
        return isinstance(other, exp.Interval) and _holds(inner, node)
    return False


def _in_own_row_filter(node: exp.Expression) -> bool:
    """Whether `node` sits in the WHERE, ON, HAVING or QUALIFY of its nearest enclosing SELECT.

    In that SELECT's select list it is a feature, however many queries are nested around it: a
    flag computed in a derived table that is then joined still reads the future.
    """
    child = node
    while child.parent is not None and not isinstance(child.parent, exp.Select):
        child = child.parent
    if child.parent is None:
        return False
    return child.arg_key in ("where", "having", "qualify", "joins")


def _compared_with_spine_time(node: exp.Subquery, spine: set[str]) -> bool:
    """Whether the subquery is a bound on the spine's own `label_ts` in a row filter."""
    comparison = node.find_ancestor(*_CUTOFF_COMPARISONS)
    if comparison is None or not _in_own_row_filter(comparison):
        return False
    if isinstance(comparison, exp.Between):
        bounds = (comparison.args.get("low"), comparison.args.get("high"))
        return _is_spine_time(comparison.this, spine) and any(
            b is not None and _holds(b, node) for b in bounds
        )
    left, right = comparison.this, comparison.expression
    if _holds(left, node):
        return _is_spine_time(right, spine)
    return _holds(right, node) and _is_spine_time(left, spine)


def fix_spine_cutoffs(
    sql: str, conn, relations: Mapping[str, str] | None = None
) -> tuple[str, list[str]]:
    """`sql` with each time-split cutoff replaced by the value it has on the final spine.

    A cutoff is `label_ts` of the spine compared (`<`, `<=`, `>`, `>=`, `BETWEEN`) with an
    uncorrelated `(SELECT MAX|MIN|quantile|median(label_ts) FROM labels)`, with at most an interval
    added or subtracted outside it, in a WHERE, ON, HAVING or QUALIFY. The documented rules accept
    it by design: a split is chosen before any prediction time exists. Its value is computed once
    on the final tables and written into the query as a literal, so the final run and the run as
    of `t` use the same cutoff and everything else in the query is audited as it is. A subquery used
    any other way (on `event_ts` or `ingested_at`, in a feature, pinned with `=`, correlated, with
    arithmetic inside) stays as written. Returns the new SQL and the cutoff values.
    """
    tree = parse_one(sql, read="duckdb")
    relations = relations or {}
    ctes = _cte_names(tree)
    spine = _spine_names(tree, relations, ctes)
    fixed: list[str] = []
    for node in list(tree.find_all(exp.Subquery)):
        if not _is_spine_cutoff(node, relations, ctes):
            continue
        if not _compared_with_spine_time(node, spine):
            continue
        rows = conn.sql(node.this.sql(dialect="duckdb")).fetchall()
        value = rows[0][0] if rows else None
        if not isinstance(value, datetime):
            continue
        literal = f"TIMESTAMP '{value.isoformat(sep=' ')}'"
        node.replace(parse_one(literal, read="duckdb"))
        fixed.append(value.isoformat(sep=" "))
    return (tree.sql(dialect="duckdb") if fixed else sql), fixed


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
    sqls: dict[str, str] = {}  # what is run: the rendering with its split cutoffs fixed
    fixed_cutoffs: dict[str, list[str]] = {}
    for case in cases:
        try:
            if reads_the_clock(case.sql):
                results[case.case_id] = CaseResult(
                    case.case_id, "unauditable", "reads the wall clock (the audit cannot see L7)"
                )
                continue
            sql, cutoffs = fix_spine_cutoffs(case.sql, final_conn, relations)
            sqls[case.case_id] = sql
            if cutoffs:
                fixed_cutoffs[case.case_id] = cutoffs
            final[case.case_id] = _run(final_conn, sql)
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
                got_columns, got_rows = _run(conn, sqls[case.case_id])
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
    for case_id, values in fixed_cutoffs.items():
        results[case_id].cutoffs = values
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
    cutoffs = sorted({c for r in verdicts for c in r.cutoffs})
    if cutoffs:
        warnings.append(
            "split cutoff fixed at " + ", ".join(cutoffs) + " (computed once from the final spine "
            "and used in both runs; the rest of the query is audited as written)"
        )
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

    audit_world = AuditWorld.from_catalog(gen.synthetic_world(args.warehouse, args.seed))
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
    undecided = Counter(r.label for r in results if r.status == "not_auditable")
    if undecided:
        by_label = ", ".join(f"{label} {n}" for label, n in sorted(undecided.items()))
        print(f"  not auditable by label: {by_label} (their labels are the authors' alone)")
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
