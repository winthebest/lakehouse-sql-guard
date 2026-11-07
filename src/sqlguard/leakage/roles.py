"""Which output columns a node of the analyzed query feeds, and what that means for a finding.

A window that looks ahead, a wall-clock call or a use of the label is only a leak when what it
computes reaches a feature. The same expression that builds a documented label column
(`LEAD(close, 1) AS next_day_return`) or a run-metadata column (`CURRENT_TIMESTAMP AS built_at`)
is not one. The spec says which output columns those are (`output_roles`); this module traces a
node through the projections, CTEs, derived tables, subqueries and UNION branches that carry its
value to the output, and a finding is demoted to INFO with weight 0 only when every output it
reaches is a declared label or metadata column (or outside a declared feature list) and the node
decides no row on the way.

Fail closed: a node is "row level" when it sits in (or feeds a column read by) a WHERE, HAVING,
QUALIFY, JOIN, GROUP BY, DISTINCT, a window's PARTITION BY / ORDER BY, a bounded ORDER BY, a set
operation that compares whole rows, or anything the trace does not understand. A trace that
reaches no output at all proves nothing and demotes nothing.
"""

from __future__ import annotations

import weakref
from collections import defaultdict
from dataclasses import dataclass, replace

from sqlglot import exp
from sqlglot.optimizer.scope import Scope, ScopeType

from sqlguard.core.findings import Finding, Severity
from sqlguard.leakage.analysis import QueryAnalysis
from sqlguard.leakage.spec import LeakageSpec


@dataclass(frozen=True)
class Attribution:
    """What a node feeds.

    `outputs`: lowercase names of the output columns of the query (every UNION branch lined up by
    position) whose value depends on the node. `row_level`: the node may decide which rows exist,
    are joined, grouped or kept; also true when the trace could not be completed.
    """

    outputs: frozenset[str] = frozenset()
    row_level: bool = True


_UNKNOWN = Attribution()
_BODY = (ScopeType.ROOT, ScopeType.CTE, ScopeType.DERIVED_TABLE, ScopeType.SET_OPERATION)
# a function that turns one row into several: the value it reads decides how many rows there are
_ROW_MULTIPLIERS = (
    exp.Unnest,
    exp.Explode,
    exp.ExplodeOuter,
    exp.Posexplode,
    exp.PosexplodeOuter,
    exp.Inline,
    exp.GenerateSeries,
    exp.ExplodingGenerateSeries,
)


class _Trace:
    """One walk from a node to the output columns it reaches."""

    def __init__(self, flow: _Flow) -> None:
        self.flow = flow
        self.outputs: set[str] = set()
        self.row_level = False
        self.failed = False
        self.pending: list[exp.Expression] = []
        self.seen_nodes: set[int] = set()
        self.seen_sinks: set[tuple[int, int]] = set()

    def run(self, start: exp.Expression) -> Attribution:
        self.push(start)
        while self.pending:
            node = self.pending.pop()
            scope = self.flow.resolver.scope_of(node)
            if scope is None:  # inside a scope no output depends on
                return _UNKNOWN
            self.step(node, scope)
            if self.failed:
                return _UNKNOWN
        return Attribution(frozenset(self.outputs), self.row_level)

    def push(self, node: exp.Expression | None) -> None:
        if node is None:
            self.failed = True
        elif id(node) not in self.seen_nodes:
            self.seen_nodes.add(id(node))
            self.pending.append(node)

    def step(self, node: exp.Expression, scope: Scope) -> None:
        """Follow `node` out of the clause of `scope` that holds it."""
        select = scope.expression
        if scope.scope_type is ScopeType.SUBQUERY:
            # whatever happens inside a subquery changes the value the subquery has where it is
            # written; that place decides what it means
            self.push(select.parent)
            return
        if scope.scope_type not in _BODY or not isinstance(select, exp.Select):
            self.row_level = True  # lateral / table function: not traced
            return
        child, parent = node, node.parent
        while parent is not None and parent is not select:
            if isinstance(parent, exp.Window) and child.arg_key != "this":
                self.row_level = True  # a partition or order key decides which rows a frame holds
            if isinstance(parent, _ROW_MULTIPLIERS):
                self.row_level = True
            child, parent = parent, parent.parent
        if parent is None:
            self.row_level = True
            return
        key = child.arg_key
        if key == "expressions":
            self.projection(scope, select, child)
        elif key == "order":
            # an order cuts rows when the select is bounded, or when a later scope can bound it
            if _bounded(select) or scope.scope_type is not ScopeType.ROOT:
                self.row_level = True
        else:
            self.row_level = True  # where / having / qualify / join / group / from / ...

    def projection(self, scope: Scope, select: exp.Select, item: exp.Expression) -> None:
        index = next(i for i, p in enumerate(select.expressions) if p is item)
        group = select.args.get("group")
        if select.args.get("distinct") or (group is not None and group.args.get("all")):
            # which rows survive (or how they group) depends on every projected value
            self.row_level = True
        order = select.args.get("order")
        if (
            order is not None
            and any(
                isinstance(o.this, exp.Var) and o.this.name.upper() == "ALL"
                for o in order.expressions
            )
            and (_bounded(select) or scope.scope_type is not ScopeType.ROOT)
        ):
            self.row_level = True  # ORDER BY ALL orders by every column and a bound keeps the first
        name = item.alias_or_name.lower()
        # a bare name that is not a table column is another projection's alias (the qualifier
        # expands them in most clauses, not in ORDER BY)
        for column in select.find_all(exp.Column):
            if not column.table and column.name.lower() == name:
                self.push(column)
        self.emit(scope, index)

    def emit(self, scope: Scope, index: int) -> None:
        """The projection `index` of `scope` is a column of the relation `scope` produces."""
        if (id(scope), index) in self.seen_sinks:
            return
        self.seen_sinks.add((id(scope), index))
        selects = scope.expression.selects
        if index >= len(selects):
            self.row_level = True
            return
        name = selects[index].alias_or_name.lower()
        kind = scope.scope_type
        if (
            isinstance(scope.expression, exp.SetOperation)
            and scope.expression.args.get("order")
            and (_bounded(scope.expression) or kind is not ScopeType.ROOT)
        ):
            self.row_level = True  # the union's own ORDER BY ... LIMIT picks rows by value
        if [p.alias_or_name.lower() for p in selects].count(name) > 1:
            self.row_level = True  # a declared name could stand for either column
        if kind is ScopeType.SET_OPERATION:
            union = scope.parent
            operation = union.expression if union is not None else None
            if not isinstance(operation, exp.SetOperation):
                self.row_level = True
            else:
                # a branch becomes a column of the union, named by its first branch; everything
                # but UNION ALL compares whole rows, so a value in a branch decides which survive
                if not (isinstance(operation, exp.Union) and not operation.args.get("distinct")):
                    self.row_level = True
                self.emit(union, index)
        elif kind is ScopeType.ROOT:
            self.outputs.add(name)
        elif kind is ScopeType.SUBQUERY:
            self.push(scope.expression.parent)
        elif kind in (ScopeType.CTE, ScopeType.DERIVED_TABLE):
            self.consume(scope, name)
        else:
            self.row_level = True

    def consume(self, produced: Scope, name: str) -> None:
        """Follow column `name` of a CTE / derived table into every scope that reads it."""
        for consumer, alias in self.flow.consumers.get(id(produced), ()):
            for node in consumer.expression.find_all(
                exp.Column, exp.Columns, exp.Star, exp.Identifier
            ):
                if isinstance(node, exp.Column) and node.table.lower() == alias:
                    if node.name.lower() == name and self.flow.defines(node, alias) is consumer:
                        self.push(node)
                elif _reads_whole_row(node, alias):
                    self.row_level = True  # the relation's row is read as one value
                elif isinstance(node, exp.Columns) or (
                    isinstance(node, exp.Star) and not isinstance(node.parent, exp.Count)
                ):
                    self.row_level = True  # columns picked by pattern, or a star left unexpanded


def _reads_whole_row(node: exp.Expression, alias: str) -> bool:
    """A column with no qualifier, or a bare identifier, that names the relation itself (`c`
    in `to_json(c)` or `SELECT c AS feat`), not a definition or a qualifier of something."""
    if isinstance(node, exp.Column):
        return not node.table and node.name.lower() == alias
    if not isinstance(node, exp.Identifier) or node.name.lower() != alias:
        return False
    parent = node.parent
    if isinstance(parent, exp.Alias):
        return node.arg_key != "alias"
    return not isinstance(parent, exp.Column | exp.Table | exp.TableAlias | exp.CTE)


def _bounded(select: exp.Expression) -> bool:
    """Does the query expression cut its rows (LIMIT / OFFSET), so that an ORDER BY matters?"""
    return bool(select.args.get("limit") or select.args.get("offset"))


class _Flow:
    """The scopes of one analysis, indexed by who reads whom, and the traces made on them."""

    def __init__(self, analysis: QueryAnalysis) -> None:
        resolver = analysis.resolver
        assert resolver is not None
        self.resolver = resolver
        # the recursive term reads the relation it defines; that read is not followed
        self.recursive = any(w.args.get("recursive") for w in analysis.qualified.find_all(exp.With))
        self.consumers: dict[int, list[tuple[Scope, str]]] = defaultdict(list)
        for scope in resolver.scope_by_id.values():
            if id(scope) in resolver.dead:
                continue
            for alias, source in scope.sources.items():
                if isinstance(source, Scope) and (
                    alias in scope.selected_sources or alias in scope.semi_or_anti_join_tables
                ):
                    self.consumers[id(source)].append((scope, alias.lower()))
        self.cache: dict[int, Attribution] = {}

    def defines(self, column: exp.Column, alias: str) -> Scope | None:
        """The scope whose FROM item `alias` the column refers to (the nearest one that has it)."""
        scope = self.resolver.scope_of(column)
        while scope is not None:
            names = {*scope.selected_sources, *scope.semi_or_anti_join_tables}
            if alias in {n.lower() for n in names}:
                return scope
            scope = scope.parent
        return None

    def attribution(self, node: exp.Expression) -> Attribution:
        if self.recursive:
            return _UNKNOWN
        found = self.cache.get(id(node))
        if found is None:
            found = self.cache[id(node)] = _Trace(self).run(node)
        return found


# one index per analysis, dropped with it (the index holds the resolver, never the analysis)
_FLOWS: dict[int, _Flow] = {}


def _renames_columns(analysis: QueryAnalysis) -> bool:
    """Does a FROM item give new names to the columns of what it reads (`st AS a(id, t)`)? The
    trace follows columns by name, so it would lose them there."""
    return any(
        isinstance(alias.parent, exp.Table) and alias.args.get("columns")
        for alias in analysis.qualified.find_all(exp.TableAlias)
    )


def _flow(analysis: QueryAnalysis) -> _Flow | None:
    if analysis.resolver is None or _renames_columns(analysis):
        return None
    flow = _FLOWS.get(id(analysis))
    if flow is None:
        flow = _FLOWS[id(analysis)] = _Flow(analysis)
        weakref.finalize(analysis, _FLOWS.pop, id(analysis), None)
    return flow


def feeds(analysis: QueryAnalysis, node: exp.Expression) -> Attribution:
    """The output columns `node` (a node of `analysis.qualified`) feeds, and whether it also
    decides rows. Unknown when the query has no name resolution to trace with."""
    flow = _flow(analysis)
    return _UNKNOWN if flow is None else flow.attribution(node)


def is_declared_non_feature(spec: LeakageSpec, column: str) -> bool:
    """The spec says this output column is a label, metadata, or not in the feature list.

    The entity key and the prediction time identify the row; they are never treated as outside
    the features, whatever the feature list says.
    """
    return spec.is_non_feature_output(column) and column.lower() not in spec.key_columns


def non_feature_only(analysis: QueryAnalysis, node: exp.Expression | None) -> bool:
    """True only when the trace of the node finished without deciding a row, and every output it
    reaches is declared not to be a feature. A node that reaches no output at all counts as well,
    but only for a spec that declares output roles: nothing reads its value, so no feature can."""
    if node is None:
        return False
    got = feeds(analysis, node)
    if got.row_level:
        return False
    if not got.outputs:
        return _declares_roles(analysis.spec)  # read by nothing: dead, once the spec has roles
    return all(is_declared_non_feature(analysis.spec, c) for c in got.outputs)


def _declares_roles(spec: LeakageSpec) -> bool:
    roles = spec.output_roles
    return bool(roles.label or roles.metadata or roles.features)


def attribute(analysis: QueryAnalysis, finding: Finding, node: exp.Expression | None) -> Finding:
    """`finding` about `node`, demoted to INFO when it feeds only label / metadata columns, else
    annotated with the feature columns it feeds. A finding without a node is returned as is."""
    if node is None:
        return finding
    got = feeds(analysis, node)
    spec = analysis.spec
    if non_feature_only(analysis, node):
        columns = sorted(got.outputs)
        feeds_text = f"feeds only label/metadata columns: {', '.join(columns)}"
        return replace(
            finding,
            severity=Severity.INFO,
            message=f"{finding.message} ({feeds_text if columns else 'feeds no output column'})",
            evidence={**finding.evidence, "outputs": columns, "non_feature_only": True},
        )
    columns = sorted(c for c in got.outputs if not is_declared_non_feature(spec, c))
    if not columns:
        return finding
    return replace(finding, evidence={**finding.evidence, "outputs": columns})
