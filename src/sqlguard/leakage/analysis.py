"""Static temporal analysis shared by the leakage rules.

The query is qualified against the catalog schema and split into sqlglot scopes. Every
physical table reference is collected, and every *conjunctive* filter predicate (an AND-ed
term of WHERE, ON, or HAVING — never inside OR, NOT, CASE, FILTER, or a projection) is
normalized into

    <feature time column of reference R>  OP  <prediction_ts of the spine> + offset

A predicate only bounds a reference whose rows it actually filters: the reference must be
selected in the predicate's own scope (directly or through a CTE / derived table), and in
the ON clause of an outer join only the null-supplying side is filtered. A subquery that
merely mentions an outer column bounds nothing outside itself.

The spine is the label-table reference whose prediction_ts reaches the output, plus any
label reference equated to it on prediction_ts. Label references outside the spine are
treated as temporal rows (time column prediction_ts) that must be bounded against it.

A bound reached through a CTE or derived table covers only the chain of uses it was written
for, so a CTE (or a CTE nested in another) consumed twice must be bounded at both uses.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterator
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
_COMPARISONS = (exp.LT, exp.LTE, exp.GT, exp.GTE)
_MAX_PATHS = 256

Use = tuple[int, str]  # (id(scope), alias) of one CTE / derived-table use
Chain = tuple[Use, ...]  # uses from the predicate's scope down to the reference's scope


@dataclass(frozen=True)
class TimeBound:
    """`ref.column OP prediction_ts + offset_seconds` found in a filter predicate."""

    column: str
    column_role: str  # time | availability
    op: str
    offset_seconds: float
    sql: str
    raw: bool  # both sides were bare columns (no cast/trunc/arithmetic on the feature side)
    via: Chain = ()  # () = written in the reference's own scope

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
    """One occurrence of a physical table in the query (one FROM item, however it is reached)."""

    table: str
    alias: str
    scope: Scope
    kind: str  # label | temporal | static | outcome | unclassified
    bounds: list[TimeBound] = field(default_factory=list)
    # every chain of CTE / derived-table uses by which this reference's rows reach the root
    paths: list[Chain] = field(default_factory=lambda: [()])
    # Set once the spine is known. Label references outside the spine count as temporal rows
    # (time column: prediction_ts); spine labels have a time column but are not `temporal`.
    time_column: str | None = None
    availability_column: str | None = None
    temporal: bool = False

    @property
    def direct_bounds(self) -> list[TimeBound]:
        return [b for b in self.bounds if not b.via]

    def bound_groups(self) -> list[list[TimeBound]]:
        """Bounds that apply to each way the reference's rows reach the query output.

        A bound written for a chain of uses applies to a path when the chain is a suffix of it.
        """
        direct = self.direct_bounds
        return [
            direct + [b for b in self.bounds if b.via and path[-len(b.via) :] == b.via]
            for path in self.paths
        ]

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

    @property
    def time_columns(self) -> set[str]:
        return {c for c in (self.time_column, self.availability_column) if c}

    def describe(self) -> str:
        return f"{self.table} AS {self.alias}" if self.alias != self.table else self.table


@dataclass(frozen=True)
class LabelUse:
    """A label column used somewhere other than a passthrough or a final-row filter."""

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
        return [r for r in self.refs if r.temporal]

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


@dataclass(frozen=True)
class _Filter:
    """Where a conjunctive predicate sits: a WHERE / HAVING, or the ON of a join."""

    join: exp.Join | None = None

    def filters(self, alias: str) -> bool:
        """Does the predicate restrict the rows of the FROM item named `alias`?"""
        if self.join is None:
            return True
        side = str(self.join.args.get("side") or "").upper()
        kind = str(self.join.args.get("kind") or "").upper()
        own = self.join.alias_or_name
        if side == "FULL":
            return False
        if side == "LEFT" or kind in ("SEMI", "ANTI"):
            return alias == own  # only the null-supplying (joined) side loses rows
        if side == "RIGHT":
            return alias != own
        return True


def _conjunctive_filter(node: exp.Expression) -> _Filter | None:
    """The filter `node` is an AND-ed term of, or None (OR, NOT, CASE, FILTER, projections)."""
    child, parent = node, node.parent
    while isinstance(parent, exp.And | exp.Paren):
        child, parent = parent, parent.parent
    if isinstance(parent, exp.Where):
        return None if isinstance(parent.parent, exp.Filter) else _Filter()
    if isinstance(parent, exp.Having):
        return _Filter()
    if isinstance(parent, exp.Join) and parent.args.get("on") is child:
        return _Filter(parent)
    return None


def _in_filter(node: exp.Expression) -> bool:
    """True if `node` sits in a WHERE / HAVING / QUALIFY / JOIN clause of its own query."""
    parent = node.parent
    while parent is not None and not isinstance(parent, exp.Select):
        if isinstance(parent, exp.Where | exp.Having | exp.Qualify | exp.Join):
            return True
        parent = parent.parent
    return False


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


@dataclass(frozen=True)
class Resolved:
    """A time expression traced to a column of a physical table reference."""

    ref: TableRef
    column: str
    chain: Chain  # CTE / derived-table uses crossed on the way (outermost first)
    offset: float  # seconds added to the column
    trunc: float  # seconds of truncation applied to the column
    raw: bool  # no cast, truncation, or arithmetic anywhere on the path
    alias: str  # alias of the FROM item in the scope where the expression was written

    @property
    def exact(self) -> bool:
        """The expression equals the column (at most a lossless timestamp cast)."""
        return self.offset == 0 and self.trunc == 0


class _Resolver:
    def __init__(self, scopes: list[Scope], refs: dict[int, TableRef], spec: LeakageSpec) -> None:
        self.by_expression = {id(s.expression): s for s in scopes}
        self.refs = refs
        self.spec = spec

    def scope_of(self, node: exp.Expression) -> Scope | None:
        current = node
        while current is not None:
            scope = self.by_expression.get(id(current))
            if scope is not None:
                return scope
            current = current.parent
        return None

    def _source(self, scope: Scope, alias: str, local: bool):
        """(source, scope where found). `local` limits the search to the scope's own FROM items;
        otherwise enclosing scopes are searched too (a correlated reference to an outer table)."""
        if local:
            entry = scope.selected_sources.get(alias)
            if entry:
                return entry[1], scope
            if alias in scope.semi_or_anti_join_tables and alias in scope.sources:
                return scope.sources[alias], scope  # filtered by its own ON, never exposed
            return None
        current: Scope | None = scope
        while current is not None:
            source = current.sources.get(alias)
            if source is not None:
                return source, current
            current = current.parent
        return None

    def resolve_time(
        self, node: exp.Expression, scope: Scope | None, *, local: bool = False, depth: int = 0
    ) -> Resolved | None:
        """Trace a time expression to a physical column.

        Follows projections that are a shifted, cast, or truncated column (for example
        `label_ts - INTERVAL 1 DAY AS cutoff`) through CTEs and derived tables. `local`
        restricts the first hop to the scope's own FROM items: the rows a predicate filters
        are the ones selected in its scope, never an outer query's.
        """
        peeled = _peel(node)
        if peeled is None or scope is None or depth > 8:
            return None
        column, offset, trunc = peeled
        if not column.table:
            return None
        found = self._source(scope, column.table, local)
        if found is None:
            return None
        source, owner = found
        raw = column is node
        if isinstance(source, exp.Table):
            ref = self.refs.get(id(source))
            if ref is None:
                return None
            return Resolved(ref, column.name.lower(), (), offset, trunc, raw, column.table)
        if isinstance(source, Scope) and isinstance(source.expression, exp.Select):
            for projection in source.expression.selects:
                if projection.alias_or_name.lower() != column.name.lower():
                    continue
                inner = projection.this if isinstance(projection, exp.Alias) else projection
                got = self.resolve_time(inner, source, local=True, depth=depth + 1)
                if got is None:
                    return None
                return Resolved(
                    got.ref,
                    got.column,
                    ((id(owner), column.table), *got.chain),
                    offset + got.offset,
                    max(trunc, got.trunc),
                    raw and got.raw,
                    column.table,
                )
        return None

    def resolve(self, column: exp.Column, scope: Scope | None) -> Resolved | None:
        return self.resolve_time(column, scope)

    def through(self, scope: Scope, name: str) -> Resolved | None:
        """Trace the output column `name` of a SELECT scope to a physical column."""
        select = scope.expression
        if not isinstance(select, exp.Select):
            return None
        for projection in select.selects:
            if projection.alias_or_name.lower() == name.lower():
                inner = projection.this if isinstance(projection, exp.Alias) else projection
                return self.resolve_time(inner, scope, local=True)
        return None

    def prediction_label(self, node: exp.Expression, scope: Scope | None, local=False):
        """Resolve `node` when it is exactly a label table's prediction_ts, else None."""
        got = self.resolve_time(node, scope, local=local)
        if got and got.exact and got.ref.kind == "label" and got.column == self.spec.prediction_ts:
            return got
        return None


def _branches(expression: exp.Expression) -> list[exp.Expression]:
    expression = expression.unnest()
    if isinstance(expression, exp.SetOperation):
        return _branches(expression.left) + _branches(expression.right)
    return [expression]


def _scope_paths(scopes: list[Scope]) -> dict[int, list[Chain]]:
    """For every scope, each chain of CTE / derived-table uses leading from the root to it."""
    consumers: dict[int, list[tuple[Scope, str]]] = defaultdict(list)
    for scope in scopes:
        for alias, (_, source) in scope.selected_sources.items():
            if isinstance(source, Scope):
                consumers[id(source)].append((scope, alias))
    memo: dict[int, list[Chain]] = {}

    def paths(scope: Scope, seen: tuple[int, ...]) -> list[Chain]:
        if id(scope) in memo:
            return memo[id(scope)]
        found: list[Chain] = []
        for consumer, alias in consumers.get(id(scope), []):
            if id(consumer) in seen:
                continue  # recursive CTE
            found += [(*p, (id(consumer), alias)) for p in paths(consumer, (*seen, id(scope)))]
            if len(found) > _MAX_PATHS:
                raise ValueError("query reuses CTEs along too many paths to analyze")
        memo[id(scope)] = found or [()]
        return memo[id(scope)]

    return {id(scope): paths(scope, ()) for scope in scopes}


def _collect_refs(scopes: list[Scope], spec: LeakageSpec) -> dict[int, TableRef]:
    """One TableRef per physical FROM item, shared by every scope that can see it."""
    refs: dict[int, TableRef] = {}

    def add(scope: Scope, alias: str, source: exp.Table) -> None:
        if id(source) not in refs:
            name = source.name.lower()
            refs[id(source)] = TableRef(name, alias, scope, spec.classify(name))

    for scope in scopes:  # home scope: where the item is actually selected from
        for alias, (_, source) in scope.selected_sources.items():
            if isinstance(source, exp.Table):
                add(scope, alias, source)
    for scope in scopes:  # tables only reachable through a semi / anti join
        for alias, source in scope.sources.items():
            if isinstance(source, exp.Table):
                add(scope, alias, source)
    return refs


def analyze(sql: str, spec: LeakageSpec, schema: Schema) -> QueryAnalysis:
    """Qualify `sql` and attach normalized time bounds to every table reference."""
    schema = {t.lower(): {c.lower(): ty for c, ty in cols.items()} for t, cols in schema.items()}
    qualified = qualify_statement(sql, schema, spec.dialect)
    scopes = list(traverse_scope(qualified))
    refs = _collect_refs(scopes, spec)
    paths = _scope_paths(scopes)
    for ref in refs.values():
        ref.paths = paths[id(ref.scope)]

    resolver = _Resolver(scopes, refs, spec)
    analysis = QueryAnalysis(spec, schema, qualified, list(refs.values()))
    _check_spec(analysis)
    root = scopes[-1] if scopes else None
    analysis.spine = _spine(analysis, root, resolver)
    spine_ids = {id(r) for r in analysis.spine}
    for ref in analysis.refs:
        if ref.kind == "temporal":
            ref.time_column = spec.time_columns[ref.table]
            ref.availability_column = spec.availability_columns.get(ref.table)
            ref.temporal = True
        elif ref.kind == "label":
            ref.time_column = spec.prediction_ts
            ref.temporal = id(ref) not in spine_ids

    for node in qualified.find_all(exp.LT, exp.LTE, exp.GT, exp.GTE, exp.Between):
        context = _conjunctive_filter(node)
        if context is None:
            continue
        scope = resolver.scope_of(node)
        if isinstance(node, exp.Between):
            pairs = [(node.this, ">=", node.args["low"]), (node.this, "<=", node.args["high"])]
        else:
            pairs = [(node.left, _OPS[type(node)], node.right)]
        for left, op, right in pairs:
            _record(left, op, right, scope, context, resolver, spine_ids, node)
    _record_latest_row(analysis, resolver)

    outputs = _output_scopes(root, resolver) if root is not None else []
    analysis.label_uses = _label_uses(analysis, resolver, {id(s) for s in outputs}, spine_ids)
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


def _output_scopes(root: Scope, resolver: _Resolver) -> list[Scope]:
    """The SELECT scopes producing the query output (one per UNION branch); [] if unsupported."""
    found = [resolver.by_expression.get(id(b)) for b in _branches(root.expression)]
    if not found or any(s is None or not isinstance(s.expression, exp.Select) for s in found):
        return []
    return found


def _equality_side(node: exp.Expression, scope: Scope | None, resolver: _Resolver):
    """(label refs, alias) for one side of `a = b` when it is a label's exact prediction_ts.

    COALESCE(a, b) is accepted when every argument is: it then equals one of them, or NULL.
    """
    nodes = [node.this, *node.expressions] if isinstance(node, exp.Coalesce) else [node]
    got = [resolver.prediction_label(n, scope) for n in nodes]
    if not got or any(g is None for g in got):
        return None
    return [g.ref for g in got], got[0].alias if len(got) == 1 else None


def _spine(analysis: QueryAnalysis, root: Scope | None, resolver: _Resolver) -> list[TableRef]:
    spec = analysis.spec
    labels = [r for r in analysis.refs if r.kind == "label"]
    outputs = _output_scopes(root, resolver) if root is not None else []
    seeds = [
        resolver.prediction_label(_output(s, spec.prediction_ts), s, local=True) for s in outputs
    ]
    if not seeds or any(seed is None for seed in seeds):
        analysis.spec_errors.append(
            f"output column {spec.prediction_ts!r} must pass through "
            f"{spec.label_table}.{spec.prediction_ts} so rows can be checked against it"
        )
        return labels  # fall back to every label reference
    spine = {id(seed.ref): seed.ref for seed in seeds}
    equalities = []
    for node in analysis.qualified.find_all(exp.EQ):
        context = _conjunctive_filter(node)
        if context is None:
            continue
        scope = resolver.scope_of(node)
        a = _equality_side(node.left, scope, resolver)
        b = _equality_side(node.right, scope, resolver)
        if a and b:
            equalities.append((a, b, context))
    changed = True
    while changed:
        changed = False
        for a, b, context in equalities:
            for (known, _), (new, alias) in ((a, b), (b, a)):
                # a single column equal to something already on the spine is on it too — if the
                # predicate really restricts that column's rows (not the preserved side of a join)
                if (
                    len(new) == 1
                    and alias is not None
                    and context.filters(alias)
                    and all(id(r) in spine for r in known)
                    and id(new[0]) not in spine
                ):
                    spine[id(new[0])] = new[0]
                    changed = True
    return list(spine.values())


def _output(scope: Scope, name: str) -> exp.Expression:
    """The projection expression of the output column `name` (a placeholder if absent)."""
    for projection in scope.expression.selects:
        if projection.alias_or_name.lower() == name.lower():
            return projection.this if isinstance(projection, exp.Alias) else projection
    return exp.null()


def _record(left, op, right, scope, context, resolver, spine_ids, node) -> None:
    spec = resolver.spec
    for feature_node, label_node, normalized in ((left, right, op), (right, left, _FLIP[op])):
        feature = resolver.resolve_time(feature_node, scope, local=True)
        label = resolver.resolve_time(label_node, scope)
        if not feature or not label:
            continue
        ref = feature.ref
        if not ref.temporal or id(label.ref) not in spine_ids or label.column != spec.prediction_ts:
            continue
        if not context.filters(feature.alias):
            continue
        if feature.column == ref.time_column:
            role = "time"
        elif feature.column == ref.availability_column:
            role = "availability"
        else:
            continue
        # feature + f_offset OP label + l_offset  ->  feature OP label + (l_offset - f_offset)
        offset = label.offset - feature.offset
        if normalized in ("<", "<=") and feature.trunc > label.trunc:
            offset += feature.trunc  # truncating only the feature side admits later facts
        ref.bounds.append(
            TimeBound(
                feature.column,
                role,
                normalized,
                offset,
                node.sql(spec.dialect),
                raw=feature.raw and label.raw,
                via=feature.chain,
            )
        )
        return


def _record_latest_row(analysis: QueryAnalysis, resolver: _Resolver) -> None:
    """`col = (SELECT MAX(col) FROM same_table WHERE col < prediction_ts ...)` bounds `col`.

    The equality keeps only the row holding the subquery's maximum, which is at or below every
    upper bound the subquery applies to its own rows.
    """
    spec = analysis.spec
    for node in analysis.qualified.find_all(exp.EQ):
        context = _conjunctive_filter(node)
        if context is None:
            continue
        scope = resolver.scope_of(node)
        for feature_node, other in ((node.left, node.right), (node.right, node.left)):
            if not isinstance(other, exp.Subquery) or not isinstance(other.this, exp.Select):
                continue
            inner = other.this
            inner_scope = resolver.by_expression.get(id(inner))
            if len(inner.selects) != 1 or inner.args.get("group") or inner_scope is None:
                continue
            peak = inner.selects[0].unalias()
            if not isinstance(peak, exp.Max):
                continue
            sub = resolver.resolve_time(peak.this, inner_scope, local=True)
            feature = resolver.resolve_time(feature_node, scope, local=True)
            if not sub or not feature or not feature.exact or not feature.ref.temporal:
                continue
            ref = feature.ref
            if ref.table != sub.ref.table or feature.column != sub.column:
                continue
            if not context.filters(feature.alias):
                continue
            role = "time" if feature.column == ref.time_column else "availability"
            for bound in sub.ref.direct_bounds:
                if bound.is_upper and bound.column == sub.column:
                    ref.bounds.append(
                        TimeBound(
                            feature.column,
                            role,
                            bound.op,
                            bound.offset_seconds,
                            node.sql(spec.dialect),
                            raw=False,
                            via=feature.chain,
                        )
                    )


def _ancestors(node: exp.Expression, stop: exp.Expression | None) -> Iterator[exp.Expression]:
    parent = node.parent
    while parent is not None and parent is not stop:
        yield parent
        parent = parent.parent


def _label_uses(
    analysis: QueryAnalysis, resolver: _Resolver, output_scopes: set[int], spine_ids: set[int]
):
    """Label columns used to compute anything, filter a subquery, or partition a window.

    Passing a label through (possibly cast, parenthesized, or defaulted with a literal) and
    filtering or grouping the rows of a spine-only scope by it are not uses: the output-column
    lineage check still catches a label that reaches a feature.
    """
    spec = analysis.spec
    uses = []
    for column in analysis.qualified.find_all(exp.Column):
        scope = resolver.scope_of(column)
        got = resolver.resolve(column, scope)
        if not got or got.ref.kind != "label" or got.column not in spec.label_columns:
            continue
        node, parent = column, column.parent
        while isinstance(parent, exp.Paren | exp.Cast | exp.TryCast) or (
            isinstance(parent, exp.Coalesce)
            and parent.this is node
            and all(isinstance(e, exp.Literal) for e in parent.expressions)
        ):
            node, parent = parent, parent.parent
        if isinstance(parent, exp.Select) or (
            isinstance(parent, exp.Alias) and isinstance(parent.parent, exp.Select)
        ):
            continue  # passthrough projection
        if scope is not None and _filters_final_rows(
            column, scope, resolver, output_scopes, spine_ids
        ):
            continue
        uses.append(LabelUse(got.ref, got.column, (column.parent or column).sql(spec.dialect)))
    return uses


def _filters_final_rows(
    column, scope: Scope, resolver: _Resolver, output_scopes: set[int], spine_ids: set[int]
) -> bool:
    """Is `column` in a filter / grouping of the output rows (or of a spine-only subquery)?"""
    kinds = list(_ancestors(column, scope.expression))
    if any(isinstance(a, exp.Window | exp.Subquery) for a in kinds):
        return False
    clauses = (exp.Where, exp.Having, exp.Qualify, exp.Group, exp.Order)
    if not any(isinstance(a, clauses) for a in kinds):
        return False
    if id(scope) in output_scopes:
        return True
    sources = [source for _, source in scope.selected_sources.values()]
    refs = [resolver.refs.get(id(s)) for s in sources if isinstance(s, exp.Table)]
    return (
        bool(sources)
        and len(refs) == len(sources)
        and all(r is not None and r.kind == "label" and id(r) in spine_ids for r in refs)
    )


_WHOLE_PARTITION = (exp.Ntile, exp.CumeDist, exp.PercentRank)


def _frame_looks_ahead(frame: exp.Expression | None) -> bool:
    if frame is None:
        return False
    return any(
        str(frame.args.get(side) or "").upper() == "FOLLOWING"
        for side in ("start_side", "end_side")
    )


def _lag_offset_negative(func: exp.Expression) -> bool:
    offset = func.args.get("offset")
    value = _constant(offset) if offset is not None else None
    return value is not None and value < 0


def _window_issues(analysis: QueryAnalysis, resolver: _Resolver, spine_ids: set[int]):
    """Windows that let a row see later rows of a temporal table or of the label spine.

    Look-ahead: LEAD (or LAG with a negative offset), a FOLLOWING frame, or an ORDER BY time
    DESC (every row then aggregates the rows after it). Whole partition: an aggregate with no
    ORDER BY, or a distribution function (CUME_DIST, PERCENT_RANK, NTILE) whose value depends
    on the size of the partition. A window is safe when the table's rows are already strictly
    before prediction time (a bound in the table's own scope runs before the window), when it
    partitions by the table's own timestamp, or when it only derives a validity interval that
    is compared with prediction_ts.
    """
    issues = []
    for window in analysis.qualified.find_all(exp.Window):
        scope = resolver.scope_of(window)
        func = window.this
        order = list(window.args["order"].expressions) if window.args.get("order") else []
        base_ahead = (
            isinstance(func, exp.Lead)
            or (isinstance(func, exp.Lag) and _lag_offset_negative(func))
            or _frame_looks_ahead(window.args.get("spec"))
        )
        base_whole = isinstance(func, _WHOLE_PARTITION) or (
            isinstance(func, exp.AggFunc) and not order and not base_ahead
        )
        descending: dict[int, TableRef] = {}
        targets: dict[int, TableRef] = {}
        for item in order:
            got = resolver.resolve_time(item.this, scope)
            if got:
                targets[id(got.ref)] = got.ref
                if item.args.get("desc") and got.column in got.ref.time_columns:
                    descending[id(got.ref)] = got.ref
        for column in func.find_all(exp.Column):
            got = resolver.resolve(column, scope)
            if got:
                targets[id(got.ref)] = got.ref
        if not targets and scope is not None:  # COUNT(*): every source row counts
            targets = {id(r): r for r in analysis.refs if r.scope is scope}
        for key, ref in targets.items():
            if ref.time_column is None:
                continue  # static, outcome, or unclassified tables have no time to look past
            ahead = base_ahead or key in descending
            if not (ahead or base_whole):
                continue
            if ref.temporal and any(b.is_upper and b.is_strict_past for b in ref.direct_bounds):
                continue  # the window only sees rows already known at prediction time
            if _partitioned_by_time(window, scope, ref, resolver):
                continue
            if ahead and _validity_interval(window, scope, ref, analysis, resolver, spine_ids):
                continue
            kind = "lookahead" if ahead else "whole_partition"
            issues.append(WindowIssue(ref, kind, window.sql(analysis.spec.dialect)))
    return issues


def _partitioned_by_time(window, scope, ref: TableRef, resolver: _Resolver) -> bool:
    """Each partition holds rows of one instant, so nothing in it lies in the future."""
    for item in window.args.get("partition_by") or []:
        got = resolver.resolve_time(item, scope)
        if got and got.ref is ref and got.exact and got.column in ref.time_columns:
            return True
    return False


def _validity_interval(window, scope, ref, analysis, resolver, spine_ids) -> bool:
    """`LEAD(time) OVER (...) AS valid_to` used only to test validity against prediction_ts.

    The next version's timestamp is compared with prediction_ts (or tested for NULL) and never
    reaches an output, and the table's rows are also bounded strictly before prediction time.
    """
    func, alias = window.this, window.parent
    if not isinstance(func, exp.Lead | exp.Lag):
        return False
    if not isinstance(alias, exp.Alias) or not isinstance(alias.parent, exp.Select):
        return False
    arg = resolver.resolve_time(func.this, scope, local=True)
    if not arg or arg.ref is not ref or not arg.exact or arg.column not in ref.time_columns:
        return False
    if not all(any(b.is_upper and b.is_strict_past for b in g) for g in ref.bound_groups()):
        return False
    owner = resolver.by_expression.get(id(alias.parent))
    uses = [
        column
        for column in analysis.qualified.find_all(exp.Column)
        if column.name.lower() == alias.alias.lower() and _reads_scope(column, owner, resolver)
    ]
    return bool(uses) and all(_validity_use(c, resolver) for c in uses)


def _reads_scope(column: exp.Column, target: Scope | None, resolver: _Resolver) -> bool:
    scope = resolver.scope_of(column)
    found = resolver._source(scope, column.table, local=False) if scope and column.table else None
    return found is not None and found[0] is target


def _validity_use(column: exp.Column, resolver: _Resolver) -> bool:
    node, parent = column, column.parent
    while isinstance(parent, exp.Paren):
        node, parent = parent, parent.parent
    if not _in_filter(column):
        return False
    if isinstance(parent, exp.Is):
        return isinstance(parent.expression, exp.Null)
    if not isinstance(parent, _COMPARISONS):
        return False
    other = parent.right if parent.left is node else parent.left
    got = resolver.resolve_time(other, resolver.scope_of(column))
    return got is not None and got.ref.kind == "label" and got.column == resolver.spec.prediction_ts
