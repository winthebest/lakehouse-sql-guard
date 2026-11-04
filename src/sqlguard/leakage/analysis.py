"""Static temporal analysis shared by the leakage rules.

The query is qualified against the catalog schema, split into sqlglot scopes, and every
physical table reference is collected. Each comparison predicate (in ON, WHERE, HAVING, or
an ASOF condition, at any nesting level) is normalized into the form

    <feature time column of reference R>  OP  <prediction_ts> + offset

so rules can ask, per reference, "is this table bounded strictly before prediction time?".
Columns are resolved through aliases, CTEs, derived tables, and correlated subqueries.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlglot import exp
from sqlglot.optimizer.scope import Scope, traverse_scope

from sqlguard.core.lineage import Schema, qualify_statement
from sqlguard.leakage.spec import LeakageSpec

_FLIP = {"<": ">", "<=": ">=", ">": "<", ">=": "<="}
_OPS = {exp.LT: "<", exp.LTE: "<=", exp.GT: ">", exp.GTE: ">="}
_UNIT_SECONDS = {
    "SECOND": 1,
    "MINUTE": 60,
    "HOUR": 3600,
    "DAY": 86_400,
    "WEEK": 604_800,
    "MONTH": 2_592_000,
    "YEAR": 31_536_000,
}


@dataclass(eq=False)
class TableRef:
    """One occurrence of a physical table in the query."""

    table: str
    alias: str
    scope: Scope
    node: exp.Table
    kind: str  # label | temporal | static | outcome | unclassified
    bounds: list[TimeBound] = field(default_factory=list)

    @property
    def aggregated(self) -> bool:
        """True when this reference is consumed by an aggregate or GROUP BY in its scope."""
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
class TimeBound:
    """`ref.column OP prediction_ts + offset_seconds`, with `column_role` time|availability."""

    column: str
    column_role: str
    op: str
    offset_seconds: float
    sql: str

    @property
    def is_upper(self) -> bool:
        return self.op in ("<", "<=")

    @property
    def is_strict_past(self) -> bool:
        """Upper bound that only admits facts strictly before prediction time."""
        return (self.op == "<" and self.offset_seconds <= 0) or (
            self.op == "<=" and self.offset_seconds < 0
        )


def _innermost_select(node: exp.Expression) -> exp.Expression | None:
    parent = node.parent
    while parent is not None and not isinstance(parent, exp.Select):
        parent = parent.parent
    return parent


def _interval_seconds(node: exp.Expression) -> float | None:
    if not isinstance(node, exp.Interval):
        return None
    unit = node.unit.name.upper().rstrip("S") if node.unit else "DAY"
    try:
        amount = float(node.this.name)
    except (TypeError, ValueError):
        return None
    return amount * _UNIT_SECONDS.get(unit, 86_400)


@dataclass
class QueryAnalysis:
    spec: LeakageSpec
    schema: Schema
    qualified: exp.Expression
    refs: list[TableRef]

    @property
    def temporal_refs(self) -> list[TableRef]:
        return [r for r in self.refs if r.kind == "temporal"]

    @property
    def unclassified_refs(self) -> list[TableRef]:
        return [r for r in self.refs if r.kind == "unclassified"]

    def column_type(self, table: str, column: str) -> str:
        return self.schema.get(table, {}).get(column, "").upper()


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
        """Return (TableRef, column_name) for a passthrough column, else None."""
        if scope is None or depth > 8 or not column.table:
            return None
        current: Scope | None = scope
        while current is not None:
            source = current.sources.get(column.table)
            if isinstance(source, exp.Table):
                ref = self.refs.get((id(current), column.table))
                return (ref, column.name) if ref else None
            if isinstance(source, Scope):
                return self._through(source, column.name, depth)
            current = current.parent
        return None

    def _through(self, scope: Scope, name: str, depth: int):
        select = scope.expression
        if not isinstance(select, exp.Select):
            return None
        for projection in select.selects:
            if projection.alias_or_name != name:
                continue
            inner = projection.this if isinstance(projection, exp.Alias) else projection
            if isinstance(inner, exp.Column):
                return self.resolve(inner, scope, depth + 1)
        return None


def analyze(sql: str, spec: LeakageSpec, schema: Schema) -> QueryAnalysis:
    """Qualify `sql` and attach normalized time bounds to every table reference."""
    qualified = qualify_statement(sql, schema, spec.dialect)
    scopes = list(traverse_scope(qualified))
    refs: dict[tuple[int, str], TableRef] = {}
    for scope in scopes:
        for alias, source in scope.sources.items():
            if isinstance(source, exp.Table):
                refs[(id(scope), alias)] = TableRef(
                    source.name, alias, scope, source, spec.classify(source.name)
                )
    resolver = _Resolver(scopes, refs)
    analysis = QueryAnalysis(spec, schema, qualified, list(refs.values()))

    for node in qualified.find_all(exp.LT, exp.LTE, exp.GT, exp.GTE, exp.Between):
        scope = resolver.scope_of(node)
        if isinstance(node, exp.Between):
            pairs = [(node.this, ">=", node.args["low"]), (node.this, "<=", node.args["high"])]
        else:
            pairs = [(node.left, _OPS[type(node)], node.right)]
        for left, op, right in pairs:
            _record(left, op, right, scope, resolver, spec, node)
    return analysis


def _label_offset(side: exp.Expression, scope, resolver: _Resolver, spec) -> float | None:
    """Offset in seconds if `side` is prediction_ts (+/- interval), else None."""
    sign, base, offset = 1, side, 0.0
    if isinstance(side, exp.Paren):
        return _label_offset(side.this, scope, resolver, spec)
    if isinstance(side, exp.Add | exp.Sub):
        seconds = _interval_seconds(side.expression)
        if seconds is None:
            return None
        sign = 1 if isinstance(side, exp.Add) else -1
        base, offset = side.this, sign * seconds
    if not isinstance(base, exp.Column):
        return None
    resolved = resolver.resolve(base, scope)
    if not resolved:
        return None
    ref, column = resolved
    if ref.kind == "label" and column == spec.prediction_ts:
        return offset
    return None


def _feature_column(side: exp.Expression, scope, resolver: _Resolver, spec):
    if not isinstance(side, exp.Column):
        return None
    resolved = resolver.resolve(side, scope)
    if not resolved:
        return None
    ref, column = resolved
    if ref.kind != "temporal":
        return None
    if column == spec.time_columns.get(ref.table):
        return ref, column, "time"
    if column == spec.availability_columns.get(ref.table):
        return ref, column, "availability"
    return None


def _record(left, op, right, scope, resolver, spec, node) -> None:
    for feature_side, label_side, normalized in ((left, right, op), (right, left, _FLIP[op])):
        feature = _feature_column(feature_side, scope, resolver, spec)
        offset = _label_offset(label_side, scope, resolver, spec)
        if feature and offset is not None:
            ref, column, role = feature
            ref.bounds.append(TimeBound(column, role, normalized, offset, node.sql(spec.dialect)))
            return
