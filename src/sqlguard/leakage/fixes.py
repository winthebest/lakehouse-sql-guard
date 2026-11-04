"""Point-in-time rewrites for queries the validator calls leaky, each checked before it is offered.

`suggest_fix(sql, spec, report)` repairs the query one finding at a time (`rewrite.py` holds the
edits), re-running the static analysis after every edit:

- `static_only`: the finding is gone and the edit introduced no finding;
- `static_and_replay`: also, with a catalog, the repaired query replays point-in-time (PASS, never
  UNVERIFIABLE) and the validator's verdict on it is clean;
- `unverified`: the edit did not remove its finding, replay disagrees, or nothing could be
  rewritten (L6 and L7 are only explained: removing the label or the clock changes what the
  feature means, which only the author can decide; so is an aggregate CTE grouped by entity only,
  which needs the spine as an input).

Fixes are cumulative: each `Fix` carries the query before and after its own edit, so the last
one holds the fully repaired query. Formatting is normalized (both sides are re-rendered).
Nothing here changes what `validate` reports.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field, replace
from enum import StrEnum
from typing import Any

import duckdb
from sqlglot import exp

from sqlguard.core.catalog import LakehouseCatalog
from sqlguard.core.findings import Finding
from sqlguard.core.lineage import Schema
from sqlguard.core.parsing import parse_single
from sqlguard.leakage import proof, rewrite
from sqlguard.leakage.analysis import QueryAnalysis, analyze_tree
from sqlguard.leakage.replay import Replayer, ReplayStatus
from sqlguard.leakage.spec import LeakageSpec
from sqlguard.leakage.validator import (
    LeakageReport,
    Weights,
    analysis_findings,
    static_findings,
    validate,
)

LEAK_RULES = ("L1", "L2", "L3", "L4", "L5", "L6", "L7")
_PRIORITY = {"L1": 0, "L2": 1, "L3": 2, "L5": 3, "L4": 4}
_MAX_STEPS = 12
_EXPLAIN = {
    "L6": (
        "A feature is computed from the label or from facts recorded after the outcome. No rewrite "
        "can keep its meaning: drop the feature, or compute it only from history that was "
        "already final at the prediction time (see `label_known_after`)."
    ),
    "L7": (
        "The wall clock (now(), current_date, ...) is part of a feature, so the same query gives "
        "different values on every run. Replace it with the row's prediction time."
    ),
    "L1": (
        "The query reads facts stamped after the prediction time and no local edit was found "
        "that is safe: bound the table to rows before the prediction time."
    ),
    "L2": (
        "The table is joined without a time bound relative to the prediction time. It is read "
        "through a CTE or derived table that knows nothing about the spine, so the bound has to "
        "be written where the spine and the table meet: pass the prediction time into it, or "
        "join the table directly."
    ),
    "L3": (
        "The aggregate covers the whole history of the table, including facts after the "
        "prediction time. An aggregate grouped by entity alone (in a CTE or derived table) needs "
        "the spine as input; join the table to the spine and bound it there."
    ),
    "L4": (
        "Rows can land in the table after the prediction time for events before it. Bound the "
        "availability column strictly before the prediction time."
    ),
    "L5": (
        "A bound at or past the prediction time (or compared across timezones) admits facts that "
        "were not yet known. Compare in one timezone and use a strict `<`."
    ),
}


Semantics = rewrite.Semantics


class Verification(StrEnum):
    STATIC_AND_REPLAY = "static_and_replay"
    STATIC_ONLY = "static_only"
    UNVERIFIED = "unverified"


@dataclass(frozen=True)
class Fix:
    """A rewrite for one finding. `after_sql` is None when the finding can only be explained."""

    rule: str
    location: str
    before_sql: str
    after_sql: str | None
    explanation: str
    caveats: list[str] = field(default_factory=list)
    verification: Verification = Verification.UNVERIFIED
    # Independent of `verification`, which is only about leak-freedom: a replay PASS shows the
    # repaired query reads nothing from the future, not that it computes the same thing.
    semantics: Semantics = Semantics.UNKNOWN

    @property
    def rewrites(self) -> bool:
        return self.after_sql is not None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["verification"] = self.verification.value
        data["semantics"] = self.semantics.value
        return data


@dataclass
class _Step:
    edit: rewrite.Edit
    before: str  # the query as the checker reads it (see `rewrite.render`) ...
    after: str
    shown_before: str  # ... and as the user is shown it
    shown_after: str
    verified: bool
    note: str = ""


def _render(tree: exp.Expression, dialect: str) -> str:
    return tree.sql(dialect=dialect, pretty=True)


def _key(finding: Finding) -> tuple[str, str]:
    return finding.rule_id, finding.location


def _explanation(finding: Finding, spec: LeakageSpec) -> str:
    if finding.rule_id in ("L2", "L3") and finding.location.split(" AS ")[0] == spec.label_table:
        return (
            "The label table's own rows are aggregated or filtered to decide which rows to train "
            "on, so what the query returns depends on predictions made later. Use a cutoff that "
            "does not depend on the entity, such as a fixed date."
        )
    return _EXPLAIN.get(finding.rule_id, finding.message)


def _depth(analysis: QueryAnalysis, finding: Finding) -> int:
    depths = []
    for ref in analysis.refs:
        if ref.describe() == finding.location:
            depth, scope = 0, ref.scope
            while scope.parent is not None:
                depth, scope = depth + 1, scope.parent
            depths.append(depth)
    return max(depths, default=0)


def _repair_one(ctx: rewrite.Context, finding: Finding) -> rewrite.Edit | None:
    analysis = ctx.analysis
    rule = finding.rule_id
    if rule not in _PRIORITY:
        return None
    window = finding.evidence.get("window")
    if window is not None:
        for issue in analysis.windows:
            if issue.sql == window and issue.ref.describe() == finding.location:
                return rewrite.fix_window(ctx, issue)
        return None
    if rule == "L5" and "predicates" not in finding.evidence:
        return None  # a timezone mismatch needs an explicit cast the author must choose
    for ref in analysis.refs:
        if ref.describe() != finding.location:
            continue
        if rule == "L1":
            edit = rewrite.mirror(ctx, ref)
        elif rule == "L5":
            edit = rewrite.tighten(ctx, ref)
        else:
            edit = rewrite.add_bounds(ctx, ref, rule) or rewrite.inline_latest(ctx, ref, rule)
        if edit is not None:
            return edit
    return None


def _repair(
    tree: exp.Expression, spec: LeakageSpec, schema: Schema
) -> tuple[list[_Step], list[Finding]]:
    """Apply edits until nothing fixable is left or one fails to verify.

    Returns the steps and the findings of the final query.
    """
    steps: list[_Step] = []
    skipped: set[tuple[str, str]] = set()
    text = _render(tree, spec.dialect)
    shown = rewrite.render(tree, spec.dialect)
    findings: list[Finding] = []
    for _ in range(_MAX_STEPS):
        rewrite.tag_tree(tree)
        try:
            analysis = analyze_tree(tree, spec, schema)
            findings = analysis_findings(analysis)
        except Exception:  # noqa: BLE001 - nothing can be repaired that cannot be analyzed
            return steps, findings
        todo = sorted(
            (f for f in findings if f.rule_id in _PRIORITY and _key(f) not in skipped),
            key=lambda f: (-_depth(analysis, f), _PRIORITY[f.rule_id]),
        )
        applied = None
        for finding in todo:
            trial = tree.copy()
            try:
                edit = _repair_one(rewrite.Context(trial, analysis, spec.dialect), finding)
            except Exception:  # noqa: BLE001 - a rewrite that cannot be built is no suggestion
                edit = None
            if edit is None:
                skipped.add(_key(finding))
                continue
            applied = (finding, edit, trial)
            break
        if applied is None:
            return steps, findings
        finding, edit, trial = applied
        after = _render(trial, spec.dialect)
        if after == text or _dangling(trial):
            skipped.add(_key(finding))
            continue
        if edit.proof_tag is not None:
            reason = proof.unproven(trial, edit.proof_tag, schema, spec.dialect, edit.proof_local)
            if reason:
                edit.not_shown(reason, finding.location.split(" AS ")[0])
        checked = static_findings(after, spec, schema)
        before_keys = Counter(_key(f) for f in findings)
        after_keys = Counter(_key(f) for f in checked)
        # the same table can be read twice under one name: one finding fewer is progress
        resolved = after_keys[_key(finding)] < before_keys[_key(finding)]
        added = sorted(
            k for k in after_keys if after_keys[k] > before_keys[k] and not _hint(k, finding)
        )
        verified = resolved and not added
        note = ""
        if not resolved:
            note = f"the checker still reports {finding.rule_id} on {finding.location}"
        elif added:
            note = "the edit introduces " + ", ".join(f"{r} on {loc}" for r, loc in added)
        steps.append(
            _Step(edit, text, after, shown, rewrite.render(trial, spec.dialect), verified, note)
        )
        tree, text, findings, shown = trial, after, checked, steps[-1].shown_after
        if not verified:
            return steps, findings
    return steps, findings


def _hint(key: tuple[str, str], target: Finding) -> bool:
    """A late-arrival hint (L4) on the table just repaired: the next edit settles it."""
    return key[0] == "L4" and key[1] == target.location and target.rule_id != "L4"


def _dangling(tree: exp.Expression) -> bool:
    """A column qualified by an empty name would be silently re-resolved by the analysis."""
    return any(
        isinstance(c.args.get("table"), exp.Identifier) and not c.args["table"].name
        for c in tree.find_all(exp.Column)
    )


def key_counts(replayer: Replayer | None, spec: LeakageSpec, sql: str) -> Counter | None:
    """How many times each prediction row (entity keys, prediction time) is returned on the
    current data, or None when it cannot be run or the keys are not in the output."""
    if replayer is None:
        return None
    try:
        table = replayer.current().sql(sql).to_arrow_table()
    except (duckdb.Error, ValueError):
        return None
    names = {c.lower(): c for c in table.column_names}
    if any(c not in names for c in spec.key_columns):
        return None
    columns = [table.column(names[c]).to_pylist() for c in spec.key_columns]
    return Counter(zip(*columns, strict=True))


def _row_count(replayer: Replayer | None, sql: str) -> int | None:
    if replayer is None:
        return None
    try:
        return replayer.current().sql(sql).to_arrow_table().num_rows
    except (duckdb.Error, ValueError):
        return None


def suggest_fix(
    sql: str,
    spec: LeakageSpec,
    report: LeakageReport | None = None,
    dialect: str | None = None,
    *,
    schema: Schema | None = None,
    catalog: LakehouseCatalog | None = None,
    replay: bool | Replayer = False,
    weights: Weights | None = None,
) -> list[Fix]:
    """Fixes for the leaks `report` found in `sql` (computed when not given).

    `dialect` is the dialect the query is written in and rendered back in (default: the
    spec's). With `replay` (True or a shared Replayer) and a catalog, the repaired query is also
    replayed, which is what earns `static_and_replay`.
    """
    if dialect is not None and dialect != spec.dialect:
        spec = replace(spec, dialect=dialect)
    weights = weights or Weights.load()
    if schema is None:
        if catalog is None:
            raise ValueError("suggest_fix() needs a schema or a catalog")
        schema = catalog.schema_for()
    replayer = None
    if replay:
        replayer = replay if isinstance(replay, Replayer) else None
        if replayer is None:
            if catalog is None:
                raise ValueError("replay needs a catalog with snapshot history")
            replayer = Replayer(catalog, spec)
    if report is None:
        report = validate(
            sql, spec, schema=schema, catalog=catalog, replay=replayer or False, weights=weights
        )
    leaks = [f for f in report.findings if f.rule_id in LEAK_RULES]
    if not leaks:
        return []
    try:
        tree = parse_single(sql, spec.dialect)
    except ValueError:
        return []
    if not isinstance(tree, exp.Query):
        return []
    rewrite.fold_match_conditions(tree)
    original = _render(tree, spec.dialect)
    original_shown = rewrite.render(tree, spec.dialect)
    steps, final_findings = _repair(tree, spec, schema)
    caveats_at_end: list[str] = []
    rows_changed = False
    level = Verification.STATIC_ONLY
    if steps and all(s.verified for s in steps):
        # replay can only vouch for a query nothing static is still reporting (the L4 hint aside:
        # settling it is what replay is for)
        settled = not any(f.rule_id in LEAK_RULES and f.rule_id != "L4" for f in final_findings)
        if replayer is not None and settled:
            level, extra, rows_changed = _replay_level(
                steps[-1].after, original, spec, schema, replayer, weights
            )
            caveats_at_end += extra

    fixes: list[Fix] = []
    for index, step in enumerate(steps):
        last = index == len(steps) - 1
        caveats = [*step.edit.caveats, *(caveats_at_end if last else [])]
        semantics = step.edit.semantics
        if last and rows_changed:
            semantics = Semantics.CHANGED
        if step.note:
            caveats.append(step.note)
        fixes.append(
            Fix(
                step.edit.rule,
                step.edit.location,
                step.shown_before,
                step.shown_after,
                step.edit.explanation,
                caveats,
                level if step.verified else Verification.UNVERIFIED,
                semantics,
            )
        )
    remaining = {_key(f) for f in final_findings}
    for finding in leaks:
        if _key(finding) not in remaining and steps:
            continue  # repaired, by its own edit or as a side effect of another
        explanation = _explanation(finding, spec)
        if _key(finding) not in remaining:  # the report flags it, the rewriter's reading does not
            explanation += (
                " (The rewriter, which reads the query in a normalized form, no longer sees "
                "this finding, so no rewrite was made.)"
            )
        fixes.append(
            Fix(
                finding.rule_id,
                finding.location,
                original_shown,
                None,
                f"{finding.message}. {explanation}",
                [],
                Verification.UNVERIFIED,
            )
        )
    return fixes


def _replay_level(
    after: str,
    original: str,
    spec: LeakageSpec,
    schema: Schema,
    replayer: Replayer,
    weights: Weights,
) -> tuple[Verification, list[str], bool]:
    """Replay the repaired query; also compare how many rows it returns."""
    caveats: list[str] = []
    before_rows, after_rows = _row_count(replayer, original), _row_count(replayer, after)
    rows_changed = before_rows is not None and after_rows is not None and before_rows != after_rows
    if rows_changed:
        lead = rewrite.ROWS_MAY_DROP if after_rows < before_rows else rewrite.ROWS_CHANGE
        caveats.append(
            f"{lead}on the current data the repaired query returns {after_rows} rows instead "
            f"of {before_rows}"
        )
    # the same number of rows can be other rows: compare the keys, and how often each appears
    keys_before = key_counts(replayer, spec, original)
    keys_after = key_counts(replayer, spec, after)
    if keys_before is not None and keys_after is not None and keys_before != keys_after:
        lost = keys_before.keys() - keys_after.keys()
        gained = keys_after.keys() - keys_before.keys()
        if lost:
            caveats.append(
                f"{rewrite.ROWS_MAY_DROP}on the current data {len(lost)} prediction row(s) of "
                "the original no longer appear in the repaired query"
            )
        elif not caveats:
            caveats.append(
                f"{rewrite.ROWS_CHANGE}on the current data the repaired query has "
                f"{len(gained)} prediction row(s) the original did not, or returns some more "
                "or fewer times"
            )
        rows_changed = True
    report = validate(after, spec, schema=schema, replay=replayer, weights=weights)
    result = report.replay
    if result is None or result.status is ReplayStatus.UNVERIFIABLE:
        reason = f": {result.reason}" if result and result.reason else ""
        caveats.append(f"replay could not confirm the repaired query{reason}")
        return Verification.STATIC_ONLY, caveats, rows_changed
    if result.status is ReplayStatus.MISMATCH:
        caveats.append(
            f"replay still finds {result.rows_mismatched} of {result.rows_compared} rows "
            "different from what was knowable at prediction time"
        )
        return Verification.UNVERIFIED, caveats, rows_changed
    if report.verdict != "clean":
        caveats.append(f"the validator's verdict on the repaired query is {report.verdict}")
        return Verification.STATIC_ONLY, caveats, rows_changed
    return Verification.STATIC_AND_REPLAY, caveats, rows_changed
