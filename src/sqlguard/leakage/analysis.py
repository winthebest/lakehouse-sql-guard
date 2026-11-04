"""Static temporal analysis shared by the leakage rules.

The query is qualified against the catalog schema and split into sqlglot scopes. Every
physical table reference is collected, and every *conjunctive* filter predicate (an AND-ed
term of WHERE, ON, or HAVING — never inside OR, NOT, CASE, or a projection) is normalized
into

    <feature time column of reference R>  OP  <prediction_ts of the spine> + offset

The spine is the label-table reference whose prediction_ts reaches the output, plus any
label reference equated to it on prediction_ts. Bounds against other copies of the label
table do not count. A bound reached through a CTE or derived table only covers that one use
of it, so a CTE reused twice must be bounded at both uses.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from sqlglot import exp
from sqlglot.optimizer.scope import Scope, traverse_scope

from sqlguard.core.lineage import Schema, qualify_statement
from sqlguard.leakage.spec import LeakageSpec

_FLIP = {"<": ">", "<=": ">=", ">": "<", ">=": "<="}
_OPS = {exp.LT: "<", exp.LTE: "<=", exp.GT: ">", exp.GTE: ">="}
_UNIT_SECONDS = {
    "MICROSECOND": 1e-6,
    "MILLISECOND": 1e-3,
    "SECOND": 1,
    "MINUTE": 60,
    "HOUR": 3600,
    "DAY": 86_400,
    "WEEK": 604_800,
    "MONTH": 2_678_400,  # upper bound (31 days): conservative for forward shifts
    "QUARTER": 7_948_800,
    "YEAR": 31_622_400,
}
_TIMESTAMP_TYPES = {
    exp.DataType.Type.TIMESTAMP,
    exp.DataType.Type.TIMESTAMPTZ,
    exp.DataType.Type.TIMESTAMPLTZ,
    exp.DataType.Type.TIMESTAMPNTZ,
    exp.DataType.Type.DATETIME,
}
_DATE_ADD = (exp.DateAdd, exp.TimestampAdd, exp.DatetimeAdd, exp.TsOrDsAdd)
_DATE_SUB = (exp.DateSub, exp.TimestampSub, exp.DatetimeSub)

Use = tuple[int, str]  # (id(scope), alias) of a CTE/derived-table use


@dataclass(frozen=True)
class TimeBound:
    """`ref.column OP prediction_ts + offset_seconds` found in a filter predicate."""

    column: str
    column_role: str  # time | availability
    op: str
    offset_seconds: float
    sql: str
    raw: bool  # both sides were bare columns (no cast/trunc/arithmetic on the feature side)
    via: Use | None = None

    @property
    def is_upper(self) -> bool:
        return self.op in ("<", "<=")

    @property
    def is_strict_past(self) -> bool:
        """Upper bound that only admits facts strictly before prediction time."""
        return (self.op == "<" and self.offset_seconds <= 0) or (
            self.op == "<=" and self.offset_seconds < 0
        )


@dataclass(eq=False)
class TableRef:
    """One occurrence of a physical table in the query."""

    table: str
    alias: str
    scope: Scope
    kind: str  # label | temporal | static | outcome | unclassified
    bounds: list[TimeBound] = field(default_factory=list)
    uses: list[Use] = field(default_factory=list)

    @property
    def direct_bounds(self) -> list[TimeBound]:
        return [b for b in self.bounds if b.via is None]

    def bound_groups(self) -> list[list[TimeBound]]:
        """Bounds that apply to each way the reference's rows reach the query output."""
        if not self.uses:
            return [self.direct_bounds]
        return [self.direct_bounds + [b for b in self.bounds if b.via == u] for u in self.uses]

    @property
    def aggregated(self) -> bool:
        """True when this reference is consumed by GROUP BY or a plain aggregate in its scope."""
        select = self.scope.expression
        if not isinstance(select, exp.Select):
            return False
        if select.args.get("group"):
            return True
        return any(
            _innermost_select(agg) is select
            for agg in select.find_all(exp.AggFunc)
            if agg.find_ancestor(exp.Window) is None
        )

    def describe(self) -> str:
        return f"{self.table} AS {self.alias}" if self.alias != self.table else self.table


@dataclass(frozen=True)
class LabelUse:
    """A label column used somewhere other than a passthrough or a root-level filter."""

    ref: TableRef
    column: str
    sql: str


@dataclass(frozen=True)
class WindowIssue:
    ref: TableRef
    kind: str  # lookahead | whole_partition
    sql: str


@dataclass
class QueryAnalysis:
    spec: LeakageSpec
    schema: Schema
    qualified: exp.Expression
    refs: list[TableRef]
    spine: list[TableRef] = field(default_factory=list)
    label_uses: list[LabelUse] = field(default_factory=list)
    windows: list[WindowIssue] = field(default_factory=list)
    spec_errors: list[str] = field(default_factory=list)

    @property
    def temporal_refs(self) -> list[TableRef]:
        return [r for r in self.refs if r.kind == "temporal"]

    @property
    def unclassified_refs(self) -> list[TableRef]:
        return [r for r in self.refs if r.kind == "unclassified"]

    @property
    def outcome_refs(self) -> list[TableRef]:
        return [r for r in self.refs if r.kind == "outcome"]

    def column_type(self, table: str, column: str) -> str:
        return self.schema.get(table, {}).get(column, "").upper()


def _innermost_select(node: exp.Expression) -> exp.Expression | None:
    parent = node.parent
    while parent is not None and not isinstance(parent, exp.Select):
        parent = parent.parent
    return parent


def _is_conjunctive_filter(node: exp.Expression) -> bool:
    """True if `node` is an AND-ed term of a WHERE, HAVING, or JOIN ... ON condition."""
    child, parent = node, node.parent
    while isinstance(parent, exp.And | exp.Paren):
        child, parent = parent, parent.parent
    if isinstance(parent, exp.Where | exp.Having):
        return True
    return isinstance(parent, exp.Join) and parent.args.get("on") is child


def _unit_seconds(unit: exp.Expression | None, default: str = "DAY") -> float | None:
    name = (unit.name if unit is not None else default).upper()
    name = name[:-1] if name.endswith("S") and name[:-1] in _UNIT_SECONDS else name
    return _UNIT_SECONDS.get(name)


def _amount_seconds(amount: exp.Expression, unit: exp.Expression | None) -> float | None:
    if isinstance(amount, exp.Interval):
        inner, sign = amount.this, 1.0
        while isinstance(inner, exp.Paren | exp.Neg):
            sign = -sign if isinstance(inner, exp.Neg) else sign
            inner = inner.this
        text, unit = str(inner.name).strip(), amount.unit
        if unit is None and " " in text:  # INTERVAL '7 days' / '1 day 2 hours'
            parts = text.split()
            if len(parts) % 2:
                return None
            total = 0.0
            for number, unit_name in zip(parts[::2], parts[1::2], strict=True):
                per = _unit_seconds(exp.var(unit_name))
                try:
                    total += float(number) * per if per is not None else math.nan
                except ValueError:
                    return None
            return None if math.isnan(total) else sign * total
        try:
            value = float(text)
        except (TypeError, ValueError):
            return None
        per = _unit_seconds(unit)
        return None if per is None else sign * value * per
    value = _constant(amount)
    per = _unit_seconds(unit)
    return None if value is None or per is None else value * per


def _constant(node: exp.Expression) -> float | None:
    """Evaluate a numeric literal expression (literals, sign, parentheses, * and /)."""
    if isinstance(node, exp.Paren):
        return _constant(node.this)
    if isinstance(node, exp.Neg):
        inner = _constant(node.this)
        return None if inner is None else -inner
    if isinstance(node, exp.Mul | exp.Div):
        a, b = _constant(node.this), _constant(node.expression)
        if a is None or b is None or (isinstance(node, exp.Div) and b == 0):
            return None
        return a * b if isinstance(node, exp.Mul) else a / b
    if isinstance(node, exp.Literal):
        try:
            return float(node.this)
        except ValueError:
            return None
    return None


def _peel(node: exp.Expression) -> tuple[exp.Expression, float, float] | None:
    """Strip casts, truncation, and +/- intervals.

    Returns (column-or-None-base, offset_seconds, truncation_seconds), or None when the
    expression has a shape we cannot reason about (callers then treat it as no bound).
    """
    offset, trunc = 0.0, 0.0
    while True:
        if isinstance(node, exp.Paren):
            node = node.this
        elif isinstance(node, exp.Cast | exp.TryCast):
            target = node.to.this if node.to else None
            if target in _TIMESTAMP_TYPES:
                node = node.this
            elif target == exp.DataType.Type.DATE:
                trunc = max(trunc, 86_400)
                node = node.this
            else:
                return None
        elif isinstance(node, exp.DateTrunc | exp.TimestampTrunc):
            seconds = _unit_seconds(node.args.get("unit"))
            if seconds is None:
                return None
            trunc = max(trunc, seconds)
            node = node.this
        elif isinstance(node, exp.Add | exp.Sub) and isinstance(node.expression, exp.Interval):
            seconds = _amount_seconds(node.expression, None)
            if seconds is None:
                return None
            offset += seconds if isinstance(node, exp.Add) else -seconds
            node = node.this
        elif isinstance(node, _DATE_ADD + _DATE_SUB):
            seconds = _amount_seconds(node.expression, node.args.get("unit"))
            if seconds is None:
                return None
            offset += -seconds if isinstance(node, _DATE_SUB) else seconds
            node = node.this
        else:
            break
    return (node, offset, trunc) if isinstance(node, exp.Column) else None


class _Resolver:
    def __init__(self, scopes: list[Scope], refs: dict[tuple[int, str], TableRef]) -> None:
        self.by_expression = {id(s.expression): s for s in scopes}
        self.refs = refs

    def scope_of(self, node: exp.Expression) -> Scope | None:
        current = node
        while current is not None:
            scope = self.by_expression.get(id(current))
            if scope is not None:
                return scope
            current = current.parent
        return None

    def resolve(self, column: exp.Column, scope: Scope | None, depth: int = 0):
        """Return (TableRef, column_name, via_use) for a passthrough column, else None."""
        if scope is None or depth > 8 or not column.table:
            return None
        current: Scope | None = scope
        while current is not None:
            source = current.sources.get(column.table)
            if isinstance(source, exp.Table):
                ref = self.refs.get((id(current), column.table))
                return (ref, column.name.lower(), None) if ref else None
            if isinstance(source, Scope):
                inner = self.through(source, column.name, depth)
                if inner is None:
                    return None
                ref, name, via = inner
                return ref, name, via or (id(current), column.table)
            current = current.parent
        return None

    def through(self, scope: Scope, name: str, depth: int = 0):
        select = scope.expression
        if not isinstance(select, exp.Select):
            return None
        for projection in select.selects:
            if projection.alias_or_name.lower() != name.lower():
                continue
            inner = projection.this if isinstance(projection, exp.Alias) else projection
            if isinstance(inner, exp.Column):
                return self.resolve(inner, scope, depth + 1)
        return None

    def resolve_time(self, node: exp.Expression, scope: Scope | None, depth: int = 0):
        """Like `resolve`, for a time expression: follows projections that are a shifted,
        cast, or truncated column (e.g. `label_ts - INTERVAL 1 DAY AS cutoff`).

        Returns (TableRef, column, via_use, offset_seconds, trunc_seconds, raw) or None.
        """
        peeled = _peel(node)
        if peeled is None or scope is None or depth > 8:
            return None
        column, offset, trunc = peeled
        raw = column is node
        current: Scope | None = scope
        while current is not None and column.table:
            source = current.sources.get(column.table)
            if isinstance(source, exp.Table):
                ref = self.refs.get((id(current), column.table))
                return (ref, column.name.lower(), None, offset, trunc, raw) if ref else None
            if isinstance(source, Scope):
                select = source.expression
                if not isinstance(select, exp.Select):
                    return None
                for projection in select.selects:
                    if projection.alias_or_name.lower() == column.name.lower():
                        inner = projection.this if isinstance(projection, exp.Alias) else projection
                        got = self.resolve_time(inner, source, depth + 1)
                        if got is None:
                            return None
                        ref, name, via, o, t, r = got
                        use = via or (id(current), column.table)
                        return ref, name, use, offset + o, max(trunc, t), raw and r
                return None
            current = current.parent
        return None


def analyze(sql: str, spec: LeakageSpec, schema: Schema) -> QueryAnalysis:
    """Qualify `sql` and attach normalized time bounds to every table reference."""
    schema = {t.lower(): {c.lower(): ty for c, ty in cols.items()} for t, cols in schema.items()}
    qualified = qualify_statement(sql, schema, spec.dialect)
    scopes = list(traverse_scope(qualified))
    refs: dict[tuple[int, str], TableRef] = {}
    for scope in scopes:
        for alias, source in scope.sources.items():
            if isinstance(source, exp.Table):
                name = source.name.lower()
                refs[(id(scope), alias)] = TableRef(name, alias, scope, spec.classify(name))
    for scope in scopes:
        for alias, source in scope.sources.items():
            if isinstance(source, Scope):
                for ref in refs.values():
                    if ref.scope is source:
                        ref.uses.append((id(scope), alias))

    resolver = _Resolver(scopes, refs)
    analysis = QueryAnalysis(spec, schema, qualified, list(refs.values()))
    _check_spec(analysis)
    analysis.spine = _spine(analysis, scopes[-1] if scopes else None, resolver)
    spine_ids = {id(r) for r in analysis.spine}

    for node in qualified.find_all(exp.LT, exp.LTE, exp.GT, exp.GTE, exp.Between):
        if not _is_conjunctive_filter(node):
            continue
        scope = resolver.scope_of(node)
        if isinstance(node, exp.Between):
            pairs = [(node.this, ">=", node.args["low"]), (node.this, "<=", node.args["high"])]
        else:
            pairs = [(node.left, _OPS[type(node)], node.right)]
        for left, op, right in pairs:
            _record(left, op, right, scope, resolver, spec, spine_ids, node)

    root = scopes[-1] if scopes else None
    analysis.label_uses = _label_uses(analysis, resolver, root)
    analysis.windows = _window_issues(analysis, resolver, spine_ids)
    return analysis


def _check_spec(analysis: QueryAnalysis) -> None:
    spec, schema = analysis.spec, analysis.schema
    for ref in {r.table: r for r in analysis.refs}.values():
        columns = schema.get(ref.table, {})
        needed = []
        if ref.kind == "temporal":
            needed = [spec.time_columns[ref.table]]
            if ref.table in spec.availability_columns:
                needed.append(spec.availability_columns[ref.table])
        elif ref.kind == "label":
            needed = [spec.prediction_ts, *spec.label_columns]
        for column in needed:
            if column not in columns:
                analysis.spec_errors.append(f"spec column {ref.table}.{column} not in schema")


def _ts_side(node, scope, resolver: _Resolver):
    return resolver.resolve_time(node, scope)


def _spine(analysis: QueryAnalysis, root: Scope | None, resolver: _Resolver) -> list[TableRef]:
    spec = analysis.spec
    labels = [r for r in analysis.refs if r.kind == "label"]
    seed = resolver.through(root, spec.prediction_ts) if root is not None else None
    if not seed or seed[0].kind != "label" or seed[1] != spec.prediction_ts:
        analysis.spec_errors.append(
            f"output column {spec.prediction_ts!r} must pass through "
            f"{spec.label_table}.{spec.prediction_ts} so rows can be checked against it"
        )
        return labels  # fall back to every label reference
    spine = {id(seed[0]): seed[0]}
    equalities = []
    for node in analysis.qualified.find_all(exp.EQ):
        if not _is_conjunctive_filter(node):
            continue
        scope = resolver.scope_of(node)
        sides = []
        for side in (node.left, node.right):
            peeled = _peel(side)
            got = resolver.resolve(peeled[0], scope) if peeled and not peeled[1] else None
            if got and got[0].kind == "label" and got[1] == spec.prediction_ts:
                sides.append(got[0])
        if len(sides) == 2:
            equalities.append(sides)
    changed = True
    while changed:
        changed = False
        for a, b in equalities:
            if (id(a) in spine) != (id(b) in spine):
                spine[id(a)] = a
                spine[id(b)] = b
                changed = True
    return list(spine.values())


def _record(left, op, right, scope, resolver, spec, spine_ids, node) -> None:
    for feature_node, label_node, normalized in ((left, right, op), (right, left, _FLIP[op])):
        feature = _ts_side(feature_node, scope, resolver)
        label = _ts_side(label_node, scope, resolver)
        if not feature or not label:
            continue
        ref, column, via, f_offset, f_trunc, f_raw = feature
        lref, lcolumn, _, l_offset, l_trunc, l_raw = label
        if ref.kind != "temporal" or id(lref) not in spine_ids or lcolumn != spec.prediction_ts:
            continue
        if column == spec.time_columns.get(ref.table):
            role = "time"
        elif column == spec.availability_columns.get(ref.table):
            role = "availability"
        else:
            continue
        # feature + f_offset OP label + l_offset  ->  feature OP label + (l_offset - f_offset)
        offset = l_offset - f_offset
        if normalized in ("<", "<=") and f_trunc > l_trunc:
            offset += f_trunc  # truncating only the feature side admits later facts
        ref.bounds.append(
            TimeBound(
                column,
                role,
                normalized,
                offset,
                node.sql(spec.dialect),
                raw=f_raw and l_raw,
                via=via,
            )
        )
        return


def _label_uses(analysis: QueryAnalysis, resolver: _Resolver, root: Scope | None):
    """Label columns used to compute anything, filter a subquery, or partition a window."""
    spec = analysis.spec
    uses = []
    for column in analysis.qualified.find_all(exp.Column):
        scope = resolver.scope_of(column)
        resolved = resolver.resolve(column, scope)
        if not resolved:
            continue
        ref, name, _ = resolved
        if ref.kind != "label" or name not in spec.label_columns:
            continue
        parent = column.parent
        if isinstance(parent, exp.Select):
            continue  # bare passthrough projection
        if isinstance(parent, exp.Alias) and isinstance(parent.parent, exp.Select):
            if parent.alias.lower() == name:
                continue  # passthrough under its own name
        elif (
            scope is root
            and column.find_ancestor(exp.Where, exp.Group, exp.Order) is not None
            and column.find_ancestor(exp.Window, exp.Subquery) is None
        ):
            continue  # filtering or grouping the final training set by its label
        uses.append(LabelUse(ref, name, (parent or column).sql(spec.dialect)))
    return uses


def _window_issues(analysis: QueryAnalysis, resolver: _Resolver, spine_ids: set[int]):
    issues = []
    for window in analysis.qualified.find_all(exp.Window):
        scope = resolver.scope_of(window)
        func = window.this
        spec_node = window.args.get("spec")
        looks_ahead = isinstance(func, exp.Lead) or (
            spec_node is not None
            and "FOLLOWING" in (str(spec_node.args.get("end_side") or "").upper(),)
        )
        if spec_node is not None and str(spec_node.args.get("start_side") or "").upper() == (
            "FOLLOWING"
        ):
            looks_ahead = True
        whole_partition = (
            isinstance(func, exp.AggFunc) and not window.args.get("order") and not looks_ahead
        )
        if not (looks_ahead or whole_partition):
            continue
        targets: dict[int, TableRef] = {}
        columns = list(func.find_all(exp.Column)) + list(
            (window.args.get("order") or exp.Order()).find_all(exp.Column)
        )
        for column in columns:
            got = resolver.resolve(column, scope)
            if got:
                targets[id(got[0])] = got[0]
        if not columns and scope is not None:  # COUNT(*): every source row counts
            for ref in analysis.refs:
                if ref.scope is scope:
                    targets[id(ref)] = ref
        for ref in targets.values():
            if ref.kind == "temporal":
                bounded = any(b.is_upper and b.is_strict_past for b in ref.direct_bounds)
                if whole_partition and bounded:
                    continue
            elif not (ref.kind == "label" and id(ref) in spine_ids):
                continue
            kind = "lookahead" if looks_ahead else "whole_partition"
            issues.append(WindowIssue(ref, kind, window.sql(analysis.spec.dialect)))
    return issues
