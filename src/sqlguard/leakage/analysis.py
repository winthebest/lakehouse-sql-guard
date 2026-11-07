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

Predicates are read in their common spellings: `ts < p`, `p - ts > INTERVAL 0`, `epoch(ts) <
epoch(p)`, `date_diff('day', ts, p) > 0`, a bound in QUALIFY, and `agg(x) FILTER (WHERE ts < p)`
when every aggregate that reads the table carries one. `HAVING MAX(ts) < p` is not a bound: it
drops every group that has a later row, so which rows exist depends on the future. A CTE that
is never used is ignored; a UNION in a CTE or derived table is traced branch by branch.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field, replace

from sqlglot import exp
from sqlglot.optimizer.scope import Scope, traverse_scope

from sqlguard.core.lineage import Schema, qualify_statement
from sqlguard.core.parsing import parse_single
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
# the only nodes allowed between a clock call and the comparison it takes part in
_TIME_ARITHMETIC = (
    exp.Paren,
    exp.Neg,
    exp.Add,
    exp.Sub,
    exp.Interval,
    exp.Cast,
    exp.TryCast,
    exp.DateTrunc,
    exp.TimestampTrunc,
    exp.DateDiff,
    exp.TsOrDsDiff,
    exp.TimeToUnix,
    *_DATE_ADD,
    *_DATE_SUB,
)
_TEST_NODES = (*_COMPARISONS, exp.EQ, exp.NEQ, exp.Between)
# how much longer the assumed length of a calendar unit is than its shortest real length
_UNIT_SLACK = {"MONTH": 3 * 86_400, "QUARTER": 3 * 86_400, "YEAR": 86_400}
_FIXED_UNITS = {"SECOND", "MINUTE", "HOUR", "DAY", "WEEK"}  # exact lengths, usable in date_diff
_CLOCK_NODES = (
    exp.CurrentDate,
    exp.CurrentDatetime,
    exp.CurrentTime,
    exp.CurrentTimestamp,
    exp.CurrentTimestampLTZ,
    exp.Localtime,
    exp.Localtimestamp,
)
_CLOCK_FUNCTIONS = {
    "now",
    "today",
    "curdate",
    "curtime",
    "getdate",
    "getutcdate",
    "sysdate",
    "sysdatetime",
    "sysutcdatetime",
    "systimestamp",
    "utc_date",
    "utc_time",
    "utc_timestamp",
    "localtime",
    "localtimestamp",
    "current_localtime",
    "current_localtimestamp",
    "get_current_time",
    "get_current_timestamp",
    "transaction_timestamp",
    "statement_timestamp",
    "clock_timestamp",
}
MATURED_LABELS = "__matured_labels"
# the only aggregates a time-split cutoff may be made of
_CUTOFF_AGGREGATES = (
    exp.Max,
    exp.Min,
    exp.Quantile,
    exp.PercentileCont,
    exp.PercentileDisc,
    exp.Median,
    exp.ApproxQuantile,
)
_MAX_PATHS = 256
_MAX_DEPTH = 32  # hops through nested CTEs / derived tables

Use = tuple[int, str]  # (id(scope), alias) of one CTE / derived-table use
Chain = tuple[Use, ...]  # uses from the predicate's scope down to the reference's scope


def _strict_past(op: str, offset: float, inclusive: bool) -> bool:
    """Does the upper bound `x OP prediction_ts + offset` stay below what the prediction can see?"""
    if op == "<":
        return offset <= 0
    return offset < 0 or (inclusive and offset == 0)


@dataclass(frozen=True)
class TimeBound:
    """`ref.column OP prediction_ts + offset_seconds` found in a filter predicate."""

    column: str
    column_role: str  # time | availability
    op: str
    offset_seconds: float
    sql: str
    raw: bool  # both sides were bare columns (no cast/trunc/arithmetic on the feature side)
    # the paths of the reference (see TableRef.paths) on which the bound holds, i.e. where the
    # label side is on the spine; `direct` = written in the reference's own scope
    instances: frozenset[Chain] = frozenset()
    direct: bool = True
    label_refs: frozenset[int] = frozenset()  # id() of the label TableRefs compared against
    # the true offset is at most offset_seconds + slack_up (month and year lengths vary)
    slack_up: float = 0.0
    inclusive: bool = False  # the spec lets facts stamped exactly at prediction_ts through
    # written in QUALIFY: window functions of the same query already saw the unfiltered rows
    post_window: bool = False
    # the predicate (a node of the qualified tree), for callers that rewrite the query
    node: exp.Expression | None = field(default=None, compare=False, repr=False)
    # the window that earns the bound (a running window over a merged timeline). It holds for the
    # rows that window carries and for nothing else that reads the table.
    earned_by: exp.Window | None = field(default=None, compare=False, repr=False)

    @property
    def is_upper(self) -> bool:
        return self.op in ("<", "<=")

    @property
    def is_strict_past(self) -> bool:
        """Upper bound that only admits facts strictly before prediction time."""
        return self.is_upper and _strict_past(self.op, self.offset_seconds, self.inclusive)

    def covers(self, delay_seconds: float) -> bool:
        """Is the bound still strictly in the past after adding a worst-case delay to its rows?"""
        offset = self.offset_seconds + self.slack_up + delay_seconds
        return self.is_upper and _strict_past(self.op, offset, self.inclusive)


@dataclass(eq=False)
class TableRef:
    """One occurrence of a physical table in the query (one FROM item, however it is reached)."""

    table: str
    alias: str
    scope: Scope
    kind: str  # label | temporal | static | outcome | unclassified
    node: exp.Table | None = None
    index: int = -1  # position of `node` among the tables of the qualified tree
    bounds: list[TimeBound] = field(default_factory=list)
    # every chain of CTE / derived-table uses by which this reference's rows reach the root
    paths: list[Chain] = field(default_factory=lambda: [()])
    # Set once the spine is known. A label reference is on the spine along some of its paths
    # (the output rows) and a temporal row along the others; other tables are temporal along all.
    time_column: str | None = None
    availability_column: str | None = None
    spine_paths: set[Chain] = field(default_factory=set)
    # an uncorrelated scan of the label table that only yields a cutoff time (see _global_scans)
    global_scan: bool = False

    @property
    def temporal_paths(self) -> list[Chain]:
        if self.time_column is None:
            return []
        return [p for p in self.paths if p not in self.spine_paths]

    @property
    def temporal(self) -> bool:
        return bool(self.temporal_paths)

    @property
    def on_spine(self) -> bool:
        return bool(self.spine_paths)

    @property
    def direct_bounds(self) -> list[TimeBound]:
        return [b for b in self.bounds if b.direct]

    def bound_groups(self) -> list[list[TimeBound]]:
        """Bounds that apply to each way the reference's rows reach the query output."""
        return [[b for b in self.bounds if path in b.instances] for path in self.temporal_paths]

    def strictly_bounded_in_scope(self) -> bool:
        """Every path is cut strictly before prediction time by a predicate in the ref's scope."""
        return bool(self.temporal_paths) and all(
            any(
                b.direct and not b.post_window and b.is_strict_past and path in b.instances
                for b in self.bounds
            )
            for path in self.temporal_paths
        )

    def matured_on(self, paths: list[Chain], known_after: float | None) -> bool:
        """Do the label rows that reach the query along `paths` all have final labels before
        prediction time? A path on which the reference is the spine never does: the spine's own
        label is the target."""
        return (
            bool(known_after)
            and self.kind == "label"
            and bool(paths)
            and all(
                path not in self.spine_paths
                and any(b.covers(known_after) and path in b.instances for b in self.bounds)
                for path in paths
            )
        )

    def matured(self, known_after: float | None) -> bool:
        """A non-spine label reference whose rows all have final labels before prediction time."""
        # the spine's own label is the target, whatever else reads the table
        return not self.on_spine and self.matured_on(self.temporal_paths, known_after)

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
    # the column reference in the qualified tree, so a caller can find the output it feeds
    node: exp.Expression | None = field(default=None, compare=False, repr=False)


@dataclass(frozen=True)
class WindowIssue:
    ref: TableRef
    kind: str  # lookahead | whole_partition
    sql: str
    node: exp.Window | None = field(default=None, compare=False, repr=False)


@dataclass(frozen=True)
class ClockUse:
    """A wall-clock function (`now()`, `current_date`, ...) that is not a plain spine filter."""

    sql: str
    # the function in the qualified tree, so a caller can find the output it feeds
    node: exp.Expression | None = field(default=None, compare=False, repr=False)


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
    clock_uses: list[ClockUse] = field(default_factory=list)
    # label scans that only produce a cutoff time; replay keeps the whole label table for them
    global_scans: list[TableRef] = field(default_factory=list)
    # ids of the scopes whose rows are the output rows: the output scope(s) and every scope on the
    # chain from the output prediction_ts down to the spine label table
    seed_scopes: set[int] = field(default_factory=set)
    # name resolution over the qualified tree, for callers that rewrite the query
    resolver: _Resolver | None = field(default=None, repr=False)

    @property
    def temporal_refs(self) -> list[TableRef]:
        return [r for r in self.refs if r.temporal]

    def lineage_input(self) -> tuple[exp.Expression, Schema]:
        """The tree and schema to compute column lineage on.

        Lineage cannot tell the spine's label from older rows of the same table. References whose
        labels were final at prediction time are renamed in a copy, so only the spine's label
        still counts as the target.
        """
        after = self.spec.label_known_after
        matured = [r for r in self.refs if r.index >= 0 and r.matured(after)]
        # a CTE read both as the spine and as history shares one table node between the two
        # uses: the matured uses get a copy of the CTE of their own
        shared = [
            (r, p)
            for r in self.refs
            if r.index >= 0 and r.on_spine and r.kind == "label"
            for p in r.temporal_paths
            if p and r.matured_on([p], after)
        ]
        if not matured and not shared:
            return self.qualified, self.schema
        tree = self.qualified.copy()
        tables = list(tree.find_all(exp.Table))
        for ref in matured:
            tables[ref.index].set("this", exp.to_identifier(MATURED_LABELS))
        if shared and self.resolver is not None:
            _split_shared(tree, self.qualified, self.resolver, shared)
        known = self.schema.get(self.spec.label_table, {})
        return tree, {**self.schema, MATURED_LABELS: known}

    @property
    def unclassified_refs(self) -> list[TableRef]:
        return [r for r in self.refs if r.kind == "unclassified"]

    @property
    def outcome_refs(self) -> list[TableRef]:
        return [r for r in self.refs if r.kind == "outcome"]

    def column_type(self, table: str, column: str) -> str:
        return self.schema.get(table, {}).get(column, "").upper()


def _split_shared(
    tree: exp.Expression,
    original: exp.Expression,
    resolver: _Resolver,
    uses: list[tuple[TableRef, Chain]],
) -> None:
    """Give each use of a label table that goes through CTEs its own copy of those CTEs in
    `tree` (a copy of `original`), with the label table renamed to MATURED_LABELS.

    The chain of a use lists the CTE references crossed from the output down to the table. Each
    CTE on it is copied (right after the original, under a new name), the reference that crosses it
    is pointed at the copy, and the label table inside the innermost copy is renamed. A use whose
    CTEs cannot be found is left shared, which only makes lineage stricter.
    """
    twin = dict(zip(map(id, original.walk()), tree.walk(), strict=True))
    for ref, chain in uses:
        copies: dict[int, exp.Expression] = {}  # original node id -> node inside the copy

        def locate(node: exp.Expression, copies=copies) -> exp.Expression:
            return copies.get(id(node)) or twin[id(node)]

        try:
            body = None
            for position, (scope_id, alias) in enumerate(chain):
                consumer = resolver.scope_by_id[scope_id]
                reference, source = consumer.selected_sources[alias]
                definition = source.expression.parent
                if not isinstance(definition, exp.CTE):
                    continue  # a derived table is used once: it needs no copy
                name = f"{definition.alias}__matured{position}_{ref.index}"
                original_cte = locate(definition)
                clone = original_cte.copy()
                clone.set("alias", exp.TableAlias(this=exp.to_identifier(name)))
                # right after the original: a CTE sees only the ones defined before it
                siblings = original_cte.parent.expressions
                siblings.insert(
                    next(i for i, c in enumerate(siblings) if c is original_cte) + 1, clone
                )
                clone.parent = original_cte.parent
                for old, new in zip(definition.walk(), clone.walk(), strict=True):
                    copies[id(old)] = new
                locate(reference).set("this", exp.to_identifier(name))
                body = clone
            if body is not None:
                locate(ref.node).set("this", exp.to_identifier(MATURED_LABELS))
        except (KeyError, AttributeError, ValueError):
            continue


def _innermost_select(node: exp.Expression) -> exp.Expression | None:
    parent = node.parent
    while parent is not None and not isinstance(parent, exp.Select):
        parent = parent.parent
    return parent


@dataclass(frozen=True)
class _Filter:
    """Where a conjunctive predicate sits: a WHERE / HAVING / QUALIFY, the ON of a join, or the
    WHERE of an aggregate's FILTER clause."""

    join: exp.Join | None = None
    kind: str = "where"  # where | having | qualify | on | filter
    aggregate_filter: exp.Filter | None = None

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
    """The filter `node` is an AND-ed term of, or None (OR, NOT, CASE, projections)."""
    child, parent = node, node.parent
    while isinstance(parent, exp.And | exp.Paren):
        child, parent = parent, parent.parent
    if isinstance(parent, exp.Where):
        if isinstance(parent.parent, exp.Filter):
            return _Filter(kind="filter", aggregate_filter=parent.parent)
        return _Filter()
    if isinstance(parent, exp.Having):
        return _Filter(kind="having")
    if isinstance(parent, exp.Qualify):
        return _Filter(kind="qualify")
    if isinstance(parent, exp.Join) and child in (
        parent.args.get("on"),
        parent.args.get("match_condition"),  # Snowflake ASOF: the inequality sits beside ON
    ):
        return _Filter(parent, "on")
    return None


def _row_filter(node: exp.Expression) -> _Filter | None:
    """Like `_conjunctive_filter`, but only for predicates that restrict the rows of a FROM item
    before any aggregate or window sees them (not FILTER clauses, not QUALIFY)."""
    context = _conjunctive_filter(node)
    return context if context is not None and context.kind in ("where", "having", "on") else None


def _in_filter(node: exp.Expression) -> bool:
    """True if `node` sits in a WHERE / HAVING / QUALIFY / JOIN clause of its own query."""
    parent = node.parent
    while parent is not None and not isinstance(parent, exp.Select):
        if isinstance(parent, exp.Where | exp.Having | exp.Qualify | exp.Join):
            return True
        parent = parent.parent
    return False


def _unit_name(unit: exp.Expression | None, default: str = "DAY") -> str:
    name = (unit.name if unit is not None else default).upper()
    return name[:-1] if name.endswith("S") and name[:-1] in _UNIT_SECONDS else name


def _unit_seconds(unit: exp.Expression | None, default: str = "DAY") -> float | None:
    return _UNIT_SECONDS.get(_unit_name(unit, default))


def _amount_seconds(
    amount: exp.Expression, unit: exp.Expression | None
) -> tuple[float, float] | None:
    """(seconds, slack) of an interval amount: slack is how much shorter the real length may be
    (months and years vary; a unitless INTERVAL '5' means seconds in DuckDB, so it is unbounded)."""
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
            total = slack = 0.0
            for number, unit_name in zip(parts[::2], parts[1::2], strict=True):
                got = _amount_seconds(
                    exp.Interval(this=exp.Literal.number(number), unit=exp.var(unit_name)), None
                )
                if got is None:
                    return None
                total += got[0]
                slack += got[1]
            return sign * total, slack
        try:
            value = float(text)
        except (TypeError, ValueError):
            return None
        per = _unit_seconds(unit)
        if per is None:
            return None
        slack = _UNIT_SLACK.get(_unit_name(unit), 0.0) * abs(value)
        return sign * value * per, math.inf if amount.unit is None else slack
    value = _constant(amount)
    per = _unit_seconds(unit)
    if value is None or per is None:
        return None
    return value * per, _UNIT_SLACK.get(_unit_name(unit), 0.0) * abs(value)


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


@dataclass(frozen=True)
class _Peeled:
    column: exp.Column
    offset: float = 0.0
    trunc: float = 0.0
    up: float = 0.0  # the true offset is at most offset + up
    down: float = 0.0  # ... and at least offset - down
    epoch: bool = False  # went through epoch(): seconds since 1970


def _peel(node: exp.Expression) -> _Peeled | None:
    """Strip casts, truncation, epoch(), and +/- intervals.

    Returns the base column with the net offset and truncation, or None when the expression has
    a shape we cannot reason about (callers then treat it as no bound).
    """
    offset = trunc = up = down = pending = 0.0
    epoch = False
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
            got = _amount_seconds(node.expression, None)
            if got is None:
                return None
            sign = 1 if isinstance(node, exp.Add) else -1
            offset, up, down = _shift(offset, up, down, sign * got[0], got[1])
            node = node.this
        elif isinstance(node, _DATE_ADD + _DATE_SUB):
            got = _amount_seconds(node.expression, node.args.get("unit"))
            if got is None:
                return None
            sign = -1 if isinstance(node, _DATE_SUB) else 1
            offset, up, down = _shift(offset, up, down, sign * got[0], got[1])
            node = node.this
        elif isinstance(node, exp.TimeToUnix) or (
            isinstance(node, exp.Extract) and node.this.name.upper() == "EPOCH"
        ):
            epoch = True
            offset += pending
            pending = 0.0
            node = node.this if isinstance(node, exp.TimeToUnix) else node.expression
        elif isinstance(node, exp.Add | exp.Sub) and _constant(node.expression) is not None:
            amount = _constant(node.expression)
            pending += amount if isinstance(node, exp.Add) else -amount
            node = node.this
        else:
            break
    if not isinstance(node, exp.Column) or (pending and not epoch):
        return None
    return _Peeled(node, offset, trunc, up, down, epoch)


def _shift(offset: float, up: float, down: float, seconds: float, slack: float):
    """Add a signed shift of `seconds` whose real size may be `slack` smaller in magnitude."""
    if seconds <= 0:
        return offset + seconds, up + slack, down
    return offset + seconds, up, down + slack


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
    owner: int  # id() of the scope that holds that FROM item
    up: float = 0.0  # the true offset is at most offset + up (see TimeBound.slack_up)
    down: float = 0.0  # ... and at least offset - down
    epoch: bool = False

    @property
    def exact(self) -> bool:
        """The expression equals the column (at most a lossless timestamp cast)."""
        return self.offset == 0 and self.trunc == 0


class _Resolver:
    def __init__(
        self,
        scopes: list[Scope],
        refs: dict[int, TableRef],
        spec: LeakageSpec,
        paths: dict[int, list[Chain]],
        dead: set[int],
    ) -> None:
        self.by_expression = {id(s.expression): s for s in scopes}
        self.refs = refs
        self.spec = spec
        self.paths = paths
        self.dead = dead  # ids of scopes that no output depends on (unused CTEs)
        self.scope_by_id = {id(s): s for s in scopes}
        self.spine: set[tuple[int, Chain]] = set()  # (id(label ref), path) on the output rows
        # (ref, upper bound, FILTER clause) waiting for every aggregate of the query to carry one
        self.pending: list[tuple[TableRef, TimeBound, exp.Filter, Scope | None, str]] = []

    def scope_of(self, node: exp.Expression) -> Scope | None:
        current = node
        while current is not None:
            scope = self.by_expression.get(id(current))
            if scope is not None:
                return None if id(scope) in self.dead else scope
            current = current.parent
        return None

    def _source(self, scope: Scope, alias: str, local: bool):
        """(source, scope holding it). `local` limits the search to the scope's own FROM items;
        otherwise enclosing scopes are searched too (a correlated reference to an outer table)."""
        if local:
            entry = scope.selected_sources.get(alias)
            if entry:
                return entry[1], scope
            if alias in scope.semi_or_anti_join_tables and alias in scope.sources:
                return scope.sources[alias], scope  # filtered by its own ON, never exposed
            return None
        current: Scope | None = scope
        fallback = None
        while current is not None:
            source = current.sources.get(alias)
            if source is not None:
                if alias in current.selected_sources:
                    return source, current
                # a LATERAL scope sees its siblings' sources without selecting from them
                fallback = fallback or (source, current)
            current = current.parent
        return fallback

    def resolve_all(
        self,
        node: exp.Expression,
        scope: Scope | None,
        *,
        local: bool = False,
        depth: int = 0,
        keyed: bool = False,
        known: Mapping[tuple[str, str], object] | None = None,
    ) -> list[Resolved] | None:
        """Trace a time expression to physical columns (several when a UNION feeds it).

        Follows projections that are a shifted, cast, or truncated column (for example
        `label_ts - INTERVAL 1 DAY AS cutoff`) through CTEs and derived tables, and through every
        branch of a UNION. `local` restricts the first hop to the scope's own FROM items: the
        rows a predicate filters are the ones selected in its scope, never an outer query's.
        With `keyed`, a projection that is a surrogate key of the spine row (see `_row_key`)
        resolves to the prediction_ts it contains: the key stands for the prediction.
        """
        peeled = _peel(node)
        if peeled is None or scope is None or depth > _MAX_DEPTH:
            return None
        column = peeled.column
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
            return [
                Resolved(
                    ref,
                    column.name.lower(),
                    (),
                    peeled.offset,
                    peeled.trunc,
                    raw,
                    column.table,
                    id(owner),
                    peeled.up,
                    peeled.down,
                    peeled.epoch,
                )
            ]
        if not isinstance(source, Scope):
            return None
        branches = _branch_scopes(source)
        if not branches:
            return None
        position = _position(branches[0], column.name)
        if position is None:
            return None
        constants = {
            name: value
            for (alias, name), value in {**(known or {}), **_constants(owner)}.items()
            if alias == column.table.lower()
        }
        branches = _unexcluded(branches, constants, _position_of(branches[0]))
        resolved: list[Resolved] = []
        for branch in branches:
            projections = branch.expression.selects
            if position >= len(projections):
                return None
            projection = projections[position]
            inner = projection.this if isinstance(projection, exp.Alias) else projection
            if keyed:
                inner = self._row_key(inner, branch) or inner
            inner_known = _passed_down(constants, projections)
            got = self.resolve_all(
                inner, branch, local=True, depth=depth + 1, keyed=keyed, known=inner_known
            )
            if not got:
                return None
            for g in got:
                resolved.append(
                    Resolved(
                        g.ref,
                        g.column,
                        ((id(owner), column.table), *g.chain),
                        peeled.offset + g.offset,
                        max(peeled.trunc, g.trunc),
                        raw and g.raw,
                        column.table,
                        id(owner),
                        peeled.up + g.up,
                        peeled.down + g.down,
                        peeled.epoch or g.epoch,
                    )
                )
        return resolved

    def _row_key(self, expression: exp.Expression, scope: Scope) -> exp.Column | None:
        """The prediction_ts column of a surrogate key of one label row, or None.

        A key names a prediction row only if its value depends on every entity key and on the
        prediction_ts, so the shape is an allowlist, never "these columns appear somewhere":

            key       := HASH(parts) | parts          (parts must be a concatenation)
            parts     := parts || parts | concat(parts, ...) | concat_ws('<literal>', parts, ...)
                         | '<literal>' | component | CAST(parts AS unbounded string)
            component := <column of one label reference>
                         | CAST(component AS unbounded string or timestamp)
                         | COALESCE(component, <literal>)      (also IFNULL / NVL)

        HASH is md5, sha1, sha2/sha256 or a known hash call, so the generated form of dbt's
        `generate_surrogate_key` qualifies. Anything else (UPPER, SUBSTR, CASE, COALESCE between two
        columns, a column as the separator, a cast to DATE) is not a key. Every entity key and the
        prediction_ts must each be a component.
        """
        arguments = _hash_arguments(expression)
        if arguments is None:
            if not isinstance(
                _unwrap_string_cast(expression), exp.DPipe | exp.Concat | exp.ConcatWs
            ):
                return None  # a bare column or literal is not a composite key
            arguments = [expression]
        found: list[exp.Column] = []
        if not all(_key_parts(argument, found) for argument in arguments) or not found:
            return None
        columns, refs, stamp = set(), set(), None
        for node in found:
            got = self.resolve_all(node, scope, local=True)
            if not got or any(g.ref.kind != "label" or not g.exact for g in got):
                return None
            columns |= {g.column for g in got}
            refs |= {id(g.ref) for g in got}
            if all(g.column == self.spec.prediction_ts for g in got):
                stamp = node
        whole = {self.spec.prediction_ts, *self.spec.entity_keys} <= columns
        return stamp if whole and len(refs) == 1 else None

    def resolve_time(
        self,
        node: exp.Expression,
        scope: Scope | None,
        *,
        local: bool = False,
        depth: int = 0,
        keyed: bool = False,
    ) -> Resolved | None:
        """The single physical column behind a time expression, or None (unknown or a UNION)."""
        got = self.resolve_all(node, scope, local=local, depth=depth, keyed=keyed)
        return got[0] if got and len(got) == 1 else None

    def resolve(self, column: exp.Column, scope: Scope | None) -> Resolved | None:
        return self.resolve_time(column, scope)

    def equated_times(self, node: exp.Expression, scope: Scope | None) -> list[Resolved] | None:
        """The physical time columns a column of `scope` is equal to, for a column whose own
        origin cannot be traced (a calendar produced by UNNEST, say).

        `s.updated_at = sc.midnight` in the WHERE, or the ON of the join that brings in `s`,
        makes every row of `s` that survives carry the value of `sc.midnight`, so a bound on
        `sc.midnight` bounds `s.updated_at`. Only an equality with another column of the same
        SELECT counts, and only where it restricts the rows of the column it equates.
        """
        if not isinstance(node, exp.Column) or scope is None:
            return None
        for eq in scope.expression.find_all(exp.EQ):
            context = _row_filter(eq)
            if context is None or context.kind == "having" or self.scope_of(eq) is not scope:
                continue
            for mine, other in ((eq.left, eq.right), (eq.right, eq.left)):
                if not (isinstance(mine, exp.Column) and _same_column(mine, node)):
                    continue
                got = self.resolve_all(other, scope, local=True)
                if got and all(g.ref.time_column and context.filters(g.alias) for g in got):
                    return [replace(g, raw=False) for g in got]
        return None

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

    def prediction_labels(
        self, node: exp.Expression, scope: Scope | None, local=False, keyed=False
    ) -> list[Resolved] | None:
        """Resolve `node` when it is exactly a label table's prediction_ts, else None.

        With `keyed`, a surrogate key of the spine row counts as its prediction_ts."""
        got = self.resolve_all(node, scope, local=local, keyed=keyed)
        if got and all(
            g.exact and g.ref.kind == "label" and g.column == self.spec.prediction_ts for g in got
        ):
            return got
        return None

    def prediction_label(self, node: exp.Expression, scope: Scope | None, local=False, keyed=False):
        got = self.prediction_labels(node, scope, local, keyed)
        return got[0] if got and len(got) == 1 else None

    def is_spine_time(self, node: exp.Expression, scope: Scope | None, keyed=False) -> bool:
        """Is `node` the spine's prediction_ts (in every branch, when a UNION feeds it)?"""
        got = self.prediction_labels(node, scope, keyed=keyed)
        return bool(got) and all(g.ref.on_spine for g in got)

    def full_paths(self, got: Resolved, instance: Chain) -> list[Chain]:
        """The paths of `got.ref` that a predicate sees when its scope runs as `instance`.

        A scope consumed twice runs twice; the predicate holds in each run for the rows that
        reach the output along that run's path.
        """
        if got.chain:
            base = self.paths.get(got.owner, [()])
            if instance in base:
                return [(*instance, *got.chain)]
            return [(*p, *got.chain) for p in base]
        return [instance] if instance in got.ref.paths else list(got.ref.paths)

    def on_spine(self, got: Resolved, instance: Chain) -> bool:
        return all((id(got.ref), p) in self.spine for p in self.full_paths(got, instance))


# hash functions a surrogate key may be wrapped in
_HASH_FUNCTIONS = {"md5", "sha1", "sha256", "sha512", "hash", "xxhash64", "farm_fingerprint"}


def _hash_arguments(node: exp.Expression) -> list[exp.Expression] | None:
    """The arguments of a hash call (md5, sha1, sha2, or a known function name), else None."""
    node = _unwrap_string_cast(node)
    if isinstance(node, exp.MD5 | exp.MD5Digest | exp.SHA | exp.SHA2):
        return [node.this]
    if isinstance(node, exp.Anonymous) and str(node.this).lower() in _HASH_FUNCTIONS:
        return list(node.expressions) or None
    return None


def _unwrap_string_cast(node: exp.Expression) -> exp.Expression:
    """Peel parentheses and casts to an unbounded string (dbt casts the concatenation it hashes)."""
    while isinstance(node, exp.Paren) or (
        isinstance(node, exp.Cast | exp.TryCast) and _is_string_cast(node)
    ):
        node = node.this
    return node


def _key_parts(node: exp.Expression, columns: list[exp.Column]) -> bool:
    """Does `node` match `parts` of the key grammar (see `_Resolver._row_key`)? Collects the
    columns of its components in `columns`."""
    node = _unwrap_string_cast(node)
    if isinstance(node, exp.Literal):
        return node.is_string
    if isinstance(node, exp.DPipe):
        return _key_parts(node.this, columns) and _key_parts(node.expression, columns)
    if isinstance(node, exp.ConcatWs):  # before Concat: it is a subclass
        separator, *items = node.expressions
        return (
            isinstance(separator, exp.Literal)
            and separator.is_string
            and bool(items)
            and all(_key_parts(item, columns) for item in items)
        )
    if isinstance(node, exp.Concat):
        items = [node.this, *node.expressions] if node.this is not None else node.expressions
        return bool(items) and all(_key_parts(item, columns) for item in items)
    return _key_component(node, columns)


def _key_component(node: exp.Expression, columns: list[exp.Column]) -> bool:
    while isinstance(node, exp.Paren):
        node = node.this
    if isinstance(node, exp.Cast | exp.TryCast):
        return _lossless_key_cast(node) and _key_component(node.this, columns)
    if isinstance(node, exp.Coalesce):  # COALESCE(component, literal): also IFNULL and NVL
        return (
            len(node.expressions) == 1
            and isinstance(node.expressions[0], exp.Literal)
            and _key_component(node.this, columns)
        )
    if isinstance(node, exp.Column):
        columns.append(node)
        return True
    return False


_STRING_TYPES = (exp.DataType.Type.VARCHAR, exp.DataType.Type.TEXT)


def _is_string_cast(node: exp.Cast | exp.TryCast) -> bool:
    target = node.to
    return target is not None and target.this in _STRING_TYPES and not target.expressions


def _lossless_key_cast(node: exp.Cast | exp.TryCast) -> bool:
    """A cast that keeps distinct values distinct: to an unbounded string, or to a timestamp."""
    target = node.to
    if target is None:
        return False
    if target.this in _STRING_TYPES:
        return not target.expressions  # VARCHAR(n) truncates in some dialects
    return target.this in _TIMESTAMP_TYPES


def _literal_value(node: exp.Expression) -> object | None:
    """The constant a projection or comparison operand stands for, or None."""
    # a cast is not folded: CAST(0.6 AS INT) is 1 in the engine, not 0.6
    while isinstance(node, exp.Paren):
        node = node.this
    if isinstance(node, exp.Boolean):
        return bool(node.this)
    if isinstance(node, exp.Neg):
        inner = _literal_value(node.this)
        return -inner if isinstance(inner, float) else None
    if isinstance(node, exp.Literal):
        if node.is_string:
            return str(node.this)
        try:
            return float(node.this)
        except ValueError:
            return None
    return None


def _conjuncts(node: exp.Expression | None) -> Iterator[exp.Expression]:
    if isinstance(node, exp.And):
        yield from _conjuncts(node.left)
        yield from _conjuncts(node.right)
    elif isinstance(node, exp.Paren):
        yield from _conjuncts(node.this)
    elif node is not None:
        yield node


def _constants(scope: Scope) -> dict[tuple[str, str], object]:
    """Columns of `scope`'s FROM items that its WHERE fixes to a constant: `flag = 0`, `WHERE
    is_spine`, `NOT is_spine`. Every output row of the SELECT carries these values."""
    select = scope.expression
    where = select.args.get("where") if isinstance(select, exp.Select) else None
    found: dict[tuple[str, str], object] = {}
    for term in _conjuncts(where.this if where is not None else None):
        negated = isinstance(term, exp.Not)
        column = term.this if negated else term
        if isinstance(column, exp.Column) and column.table:
            found[(column.table.lower(), column.name.lower())] = not negated
        elif isinstance(term, exp.EQ):
            for mine, other in ((term.left, term.right), (term.right, term.left)):
                value = _literal_value(other)
                if isinstance(mine, exp.Column) and mine.table and value is not None:
                    found[(mine.table.lower(), mine.name.lower())] = value
    return found


def _passed_down(
    constants: Mapping[str, object], projections: list[exp.Expression]
) -> dict[tuple[str, str], object]:
    """The constants of a SELECT's output columns, restated on the columns it copies them from."""
    found: dict[tuple[str, str], object] = {}
    for projection in projections:
        source = projection.unalias()
        if isinstance(source, exp.Column) and source.table:
            value = constants.get(projection.alias_or_name.lower())
            if value is not None:
                found[(source.table.lower(), source.name.lower())] = value
    return found


def _same_constant(a: object, b: object) -> bool:
    return isinstance(a, bool) == isinstance(b, bool) and a == b


def _position_of(scope: Scope):
    return lambda name: _position(scope, name)


def _unexcluded(branches: list[Scope], constants: Mapping[str, object], position) -> list[Scope]:
    """The UNION branches that can still produce a row when each column in `constants` must
    equal its constant. A branch whose projection of that column is a different constant is out;
    a branch that computes it is kept."""
    kept = branches
    for name, wanted in constants.items():
        index = position(name)
        if index is None:
            continue
        alive = []
        for branch in kept:
            selects = branch.expression.selects
            value = _literal_value(selects[index].unalias()) if index < len(selects) else None
            if value is None or _same_constant(value, wanted):
                alive.append(branch)
        kept = alive
    return kept


def _branch_scopes(scope: Scope) -> list[Scope]:
    """The SELECT scopes a derived table / CTE is made of: itself, or each UNION branch."""
    if isinstance(scope.expression, exp.Select):
        return [scope]
    found: list[Scope] = []
    for branch in scope.set_operation_scopes:
        inner = _branch_scopes(branch)
        if not inner:
            return []
        found += inner
    return found


def _output_names(node: exp.Expression) -> list[str]:
    """Output column names of a SELECT or set operation, or raise when they cannot be known."""
    node = node.unnest()
    while isinstance(node, exp.SetOperation):
        node = node.left.unnest()
    if not isinstance(node, exp.Select):
        raise ValueError("UNION BY NAME over an operand whose columns are unknown")
    names = [p.alias_or_name for p in node.selects]
    if not all(names) or any(isinstance(p.unalias(), exp.Star) for p in node.selects):
        raise ValueError("UNION BY NAME needs every column of every branch to be named")
    lowered = [n.lower() for n in names]
    if len(set(lowered)) != len(lowered):
        raise ValueError("UNION BY NAME over a branch with duplicate column names")
    return names


def _positional(tree: exp.Expression) -> exp.Expression:
    """Rewrite every `UNION [ALL] BY NAME` into the positional union it means.

    Columns are lined up by name (the first branch's order, then new names as they appear), and
    a branch without a name supplies NULL for it, exactly as the engine does. Every later stage
    (scopes, bounds, lineage, label uses) then reads the same columns in the same positions.
    An operand that is itself a positional union is named by its first leg, as the engine does,
    and its other legs are lined up by position. Branches whose columns cannot be named are
    refused (fail closed).
    """
    ops = [op for op in tree.find_all(exp.SetOperation) if op.args.get("by_name")]
    for op in reversed(ops):  # innermost first
        left, right = op.left.unnest(), op.right.unnest()
        left_names, right_names = _output_names(left), _output_names(right)
        canonical = list(left_names)
        known = {n.lower() for n in canonical}
        for name in right_names:
            if name.lower() not in known:
                canonical.append(name)
                known.add(name.lower())
        for operand in (left, right):
            own = _output_names(operand)
            leaves = [operand]
            while leaves:
                leaf = leaves.pop().unnest()
                if isinstance(leaf, exp.SetOperation):
                    leaves += [leaf.left, leaf.right]
                    continue
                _align(leaf, canonical, own)
        op.set("by_name", False)
    return tree


def _align(select: exp.Expression, canonical: list[str], names: list[str]) -> None:
    """Rewrite `select` to produce `canonical`; its own columns are called `names` by position."""
    if not isinstance(select, exp.Select):
        raise ValueError("UNION BY NAME over an operand that is not a SELECT")
    if len(select.selects) != len(names) or any(
        isinstance(p.unalias(), exp.Star) for p in select.selects
    ):
        raise ValueError("UNION BY NAME over a nested union with a different number of columns")
    by_name = {n.lower(): p for n, p in zip(names, select.selects, strict=True)}
    columns = []
    for name in canonical:
        projection = by_name.get(name.lower())
        value = projection.unalias() if projection is not None else exp.null()
        columns.append(exp.alias_(value.copy(), name, quoted=not name.isidentifier()))
    select.set("expressions", columns)


def _position(scope: Scope, name: str) -> int | None:
    """Index of the first projection called `name` (a UNION is positional after its first leg)."""
    for i, projection in enumerate(scope.expression.selects):
        if projection.alias_or_name.lower() == name.lower():
            return i
    return None


def _branches(expression: exp.Expression) -> list[exp.Expression]:
    expression = expression.unnest()
    if isinstance(expression, exp.SetOperation):
        return _branches(expression.left) + _branches(expression.right)
    return [expression]


def _scope_paths(scopes: list[Scope]) -> dict[int, list[Chain]]:
    """For every scope, each chain of CTE / derived-table uses leading from the root to it.

    A subquery or set-operation branch runs wherever its enclosing scope runs, so it inherits
    that scope's paths.
    """
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
        inherits = scope.is_subquery or scope.is_set_operation
        if not found and inherits and scope.parent is not None and id(scope.parent) not in seen:
            found = list(paths(scope.parent, (*seen, id(scope))))
        memo[id(scope)] = found or [()]
        return memo[id(scope)]

    return {id(scope): paths(scope, ()) for scope in scopes}


def _collect_refs(scopes: list[Scope], spec: LeakageSpec) -> dict[int, TableRef]:
    """One TableRef per physical FROM item, shared by every scope that can see it."""
    refs: dict[int, TableRef] = {}

    def add(scope: Scope, alias: str, source: exp.Table) -> None:
        if id(source) not in refs:
            name = source.name.lower()
            refs[id(source)] = TableRef(name, alias, scope, spec.classify(name), source)

    for scope in scopes:  # home scope: where the item is actually selected from
        for alias, (_, source) in scope.selected_sources.items():
            if isinstance(source, exp.Table):
                add(scope, alias, source)
    for scope in scopes:  # tables only reachable through a semi / anti join
        for alias, source in scope.sources.items():
            if isinstance(source, exp.Table):
                add(scope, alias, source)
    return refs


def _live_scopes(scopes: list[Scope]) -> set[int]:
    """Ids of the scopes the output depends on: everything reachable from the root.

    A CTE that no FROM, JOIN, or subquery uses never contributes a row, so it is left out.
    """
    if not scopes:
        return set()
    live: set[int] = set()
    stack = [scopes[-1]]
    while stack:
        scope = stack.pop()
        if id(scope) in live:
            continue
        live.add(id(scope))
        for _, source in scope.selected_sources.values():
            if isinstance(source, Scope):
                stack.append(source)
        for alias in scope.semi_or_anti_join_tables:
            if isinstance(scope.sources.get(alias), Scope):
                stack.append(scope.sources[alias])
        stack += [
            *scope.subquery_scopes,
            *scope.derived_table_scopes,
            *scope.set_operation_scopes,
            *scope.udtf_scopes,
        ]
    return live


def resolve_relations(tree: exp.Expression, relations: Mapping[str, str]) -> exp.Expression:
    """`tree` with each qualified relation name that `relations` maps read as its catalog table.

    `analytics.main.fct_events` becomes `events` when the spec maps it to `events` (or to
    `ns.events`), so schemas and the spec's table declarations, which are keyed by catalog table,
    apply to it. A name without an alias keeps the name the query used as its alias, so columns
    written `fct_events.id` still resolve. Names the mapping does not list, and bare names, are
    left alone, and so is the tree when nothing is renamed: the result is `tree` itself or a copy.
    """
    if not relations:
        return tree
    renames: list[tuple[int, str]] = []
    for position, table in enumerate(tree.find_all(exp.Table)):
        parts = (table.catalog, table.db, table.name)
        key = ".".join(p.lower() for p in parts if p)
        if table.name and table.db and key in relations:
            renames.append((position, relations[key].rsplit(".", 1)[-1]))
    if not renames:
        return tree
    tree = tree.copy()
    tables = list(tree.find_all(exp.Table))
    for position, target in renames:
        table = tables[position]
        used = table.name
        if not table.alias and used.lower() != target:
            table.set("alias", exp.TableAlias(this=exp.to_identifier(used)))
        table.set("this", exp.to_identifier(target))
        table.set("db", None)
        table.set("catalog", None)
    return tree


def analyze(sql: str, spec: LeakageSpec, schema: Schema) -> QueryAnalysis:
    """Qualify `sql` and attach normalized time bounds to every table reference."""
    return analyze_tree(parse_single(sql, spec.dialect), spec, schema)


def analyze_tree(tree: exp.Expression, spec: LeakageSpec, schema: Schema) -> QueryAnalysis:
    """`analyze` for a parsed statement. The tree is not modified; the analysis works on a copy
    whose nodes carry the same `meta` as the originals, so a caller can map them back."""
    schema = {t.lower(): {c.lower(): ty for c, ty in cols.items()} for t, cols in schema.items()}
    tree = resolve_relations(tree, spec.relations)
    if any(op.args.get("by_name") for op in tree.find_all(exp.SetOperation)):
        tree = tree.copy()  # the BY NAME rewrite works in place
    qualified = qualify_statement(_positional(tree), schema, spec.dialect)
    every_scope = list(traverse_scope(qualified))
    live = _live_scopes(every_scope)
    scopes = [s for s in every_scope if id(s) in live]
    refs = _collect_refs(scopes, spec)
    positions = {id(t): i for i, t in enumerate(qualified.find_all(exp.Table))}
    for ref in refs.values():
        ref.index = positions.get(id(ref.node), -1)
    paths = _scope_paths(scopes)
    for ref in refs.values():
        ref.paths = paths[id(ref.scope)]

    dead = {id(s) for s in every_scope} - live
    resolver = _Resolver(every_scope, refs, spec, paths, dead)
    analysis = QueryAnalysis(spec, schema, qualified, list(refs.values()), resolver=resolver)
    _check_spec(analysis)
    root = scopes[-1] if scopes else None
    _spine(analysis, root, resolver)
    for ref in analysis.refs:
        if ref.kind == "temporal":
            ref.time_column = spec.time_columns[ref.table]
            ref.availability_column = spec.availability_columns.get(ref.table)
        elif ref.kind == "label":
            # a label table that doubles as event history is bounded by its own time column
            ref.time_column = spec.time_columns.get(ref.table, spec.prediction_ts)
            ref.availability_column = spec.availability_columns.get(ref.table)
            ref.spine_paths = {p for p in ref.paths if (id(ref), p) in resolver.spine}
    analysis.spine = [r for r in analysis.refs if r.on_spine]

    for node in qualified.find_all(exp.LT, exp.LTE, exp.GT, exp.GTE, exp.Between):
        context = _conjunctive_filter(node)
        if context is None:
            continue
        scope = resolver.scope_of(node)
        if isinstance(node, exp.Between):
            terms = [
                (node.this, ">=", node.args["low"], 0.0, False),
                (node.this, "<=", node.args["high"], 0.0, False),
            ]
        else:
            terms = _comparison_terms(node)
        for left, op, right, shift, needs_epoch in terms:
            _record(left, op, right, scope, context, resolver, node, shift, needs_epoch)
    _record_latest_row(analysis, resolver)
    _record_timelines(analysis, resolver)
    _apply_filter_bounds(analysis, resolver)
    _global_scans(analysis, resolver)

    analysis.label_uses = _label_uses(analysis, resolver)
    analysis.windows = _window_issues(analysis, resolver)
    analysis.clock_uses = _clock_uses(analysis, resolver)
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
            if ref.table in spec.time_columns:
                needed.append(spec.time_columns[ref.table])
            if ref.table in spec.availability_columns:
                needed.append(spec.availability_columns[ref.table])
        for column in needed:
            if column not in columns:
                analysis.spec_errors.append(f"spec column {ref.table}.{column} not in schema")


def _output_scopes(root: Scope, resolver: _Resolver) -> list[Scope]:
    """The SELECT scopes producing the query output (one per UNION branch); [] if unsupported."""
    found = [resolver.by_expression.get(id(b)) for b in _branches(root.expression)]
    if not found or any(s is None or not isinstance(s.expression, exp.Select) for s in found):
        return []
    return found


def _prediction_terms(
    node: exp.Expression, scope: Scope | None, resolver: _Resolver, keyed: bool = False
):
    """The label prediction_ts columns `node` is made of, or None.

    A bare column (a lossless timestamp cast allowed) gives one; COALESCE(a, b, ...) gives all
    its arguments when each is one: the value is one of them (or NULL). USING and NATURAL joins
    are rewritten by the qualifier into such a COALESCE. A column fed by a UNION gives one per
    branch. With `keyed`, a surrogate key of the label row stands for its prediction_ts.
    """
    nodes = [node.this, *node.expressions] if isinstance(node, exp.Coalesce) else [node]
    found = [resolver.prediction_labels(n, scope, keyed=keyed) for n in nodes]
    if not found or any(g is None for g in found):
        return None
    return [g for group in found for g in group]


def _spine(analysis: QueryAnalysis, root: Scope | None, resolver: _Resolver) -> None:
    """Find the output rows' label references, per path of use.

    Seeds: the label columns the output prediction_ts is made of. Extension: a label reference
    equated (exactly) to a spine one on prediction_ts or on a surrogate key of the spine row,
    where the predicate restricts it. The
    same table reached along another path of use (a CTE consumed twice) is on the spine only if
    that path is aligned on prediction_ts too.
    """
    spec = analysis.spec
    outputs = _output_scopes(root, resolver) if root is not None else []
    seeds = []
    for scope in outputs:
        terms = _prediction_terms(_output(scope, spec.prediction_ts), scope, resolver)
        seeds.append((scope, terms))
    if not seeds or any(terms is None for _, terms in seeds):
        analysis.spec_errors.append(
            f"output column {spec.prediction_ts!r} must pass through "
            f"{spec.label_table}.{spec.prediction_ts} so rows can be checked against it"
        )
        for ref in analysis.refs:  # fall back to every label reference, along every path
            if ref.kind == "label":
                resolver.spine |= {(id(ref), p) for p in ref.paths}
        analysis.seed_scopes = {id(ref.scope) for ref in analysis.refs if ref.kind == "label"}
        return
    spine = resolver.spine
    for scope, terms in seeds:
        analysis.seed_scopes.add(id(scope))
        for term in terms:
            analysis.seed_scopes.add(id(term.ref.scope))
            analysis.seed_scopes.update(consumer for consumer, _ in term.chain)
            for instance in resolver.paths[id(scope)]:
                spine.update((id(term.ref), p) for p in resolver.full_paths(term, instance))
    equalities = []
    for node in analysis.qualified.find_all(exp.EQ):
        context = _row_filter(node)
        if context is None:
            continue
        scope = resolver.scope_of(node)
        # two copies of the spine joined on its surrogate key are aligned like two joined on
        # prediction_ts
        a = _prediction_terms(node.left, scope, resolver, keyed=True)
        b = _prediction_terms(node.right, scope, resolver, keyed=True)
        if a and b and scope is not None:
            equalities.append((a, b, context, scope))
    changed = True
    while changed:
        changed = False
        for a, b, context, scope in equalities:
            for known, new in ((a, b), (b, a)):
                # a single column equal to something already on the spine is on it too — if the
                # predicate really restricts that column's rows (not the preserved side of a join)
                if len(new) != 1 or not context.filters(new[0].alias):
                    continue
                for instance in resolver.paths[id(scope)]:
                    if all(resolver.on_spine(k, instance) for k in known):
                        for p in resolver.full_paths(new[0], instance):
                            if (id(new[0].ref), p) not in spine:
                                spine.add((id(new[0].ref), p))
                                changed = True


def _output(scope: Scope, name: str) -> exp.Expression:
    """The projection expression of the output column `name` (a placeholder if absent)."""
    for projection in scope.expression.selects:
        if projection.alias_or_name.lower() == name.lower():
            return projection.this if isinstance(projection, exp.Alias) else projection
    return exp.null()


def _same_column(a: exp.Column, b: exp.Column) -> bool:
    return a.table.lower() == b.table.lower() and a.name.lower() == b.name.lower()


def _unparen(node: exp.Expression) -> exp.Expression:
    while isinstance(node, exp.Paren):
        node = node.this
    return node


def _difference_seconds(bound: exp.Expression) -> tuple[float, bool] | None:
    """(seconds, needs_epoch) of the constant a difference is compared with.

    An INTERVAL is exact. A bare number only means seconds when both sides went through epoch();
    zero is the same in every unit.
    """
    bound = _unparen(bound)
    if isinstance(bound, exp.Interval):
        got = _amount_seconds(bound, None)
        return (got[0], False) if got is not None and got[1] == 0 else None
    value = _constant(bound)
    if value is None:
        return None
    return (0.0, False) if value == 0 else (value, True)


def _datediff_terms(diff: exp.Expression, bound: exp.Expression, op: str) -> list:
    """`date_diff(unit, start, end) OP k` as bounds on `start` against `end` (boundary counting).

    d = number of unit boundaries between start and end, so d > k means start lies before the
    unit that started k units before `end`: start < end - k*unit, and so on for the other three.
    """
    name = _unit_name(diff.args.get("unit"))
    k = _constant(_unparen(bound))
    if name not in _FIXED_UNITS or k is None or k != int(k):
        return []
    unit = _UNIT_SECONDS[name]
    start, end = diff.expression, diff.this
    if op == ">":
        return [(start, "<", end, -k * unit, False)]
    if op == ">=":
        return [(start, "<", end, (1 - k) * unit, False)]
    if op == "<":
        return [(start, ">", end, -k * unit, False)]
    return [(start, ">", end, -(k + 1) * unit, False)]


def _comparison_terms(node: exp.Expression) -> list:
    """Readings `(left, op, right, shift, needs_epoch)` of a comparison: `left OP right + shift`.

    The comparison itself, and when one side is a difference of two time expressions or a
    date_diff, the equivalent comparison of the two expressions.
    """
    op = _OPS[type(node)]
    terms = [(node.left, op, node.right, 0.0, False)]
    for diff, other, flipped in ((node.left, node.right, op), (node.right, node.left, _FLIP[op])):
        diff = _unparen(diff)
        if isinstance(diff, exp.Sub) and not isinstance(diff.expression, exp.Interval):
            got = _difference_seconds(other)
            if got is not None:  # X - Y OP c  ->  X OP Y + c
                terms.append((diff.this, flipped, diff.expression, got[0], got[1]))
        elif isinstance(diff, exp.DateDiff | exp.TsOrDsDiff):
            terms += _datediff_terms(diff, other, flipped)
    return terms


def _record(
    left, op, right, scope, context, resolver, node, shift: float = 0.0, needs_epoch: bool = False
) -> None:
    """Record `left OP right + shift` as a bound when one side is a feature time column and the
    other a label prediction_ts on the spine."""
    spec = resolver.spec
    orientations = ((left, right, op, shift), (right, left, _FLIP[op], -shift))
    for feature_node, label_node, normalized, signed in orientations:
        features = resolver.resolve_all(feature_node, scope, local=True)
        if not features:
            features = resolver.equated_times(feature_node, scope)
        labels = resolver.resolve_all(label_node, scope)
        if not features or not labels:
            continue
        if any(lab.ref.kind != "label" or lab.column != spec.prediction_ts for lab in labels):
            continue
        if needs_epoch and not all(f.epoch for f in features + labels):
            continue
        upper = normalized in ("<", "<=")
        recorded = False
        for feature in features:
            ref = feature.ref
            if ref.time_column is None or not context.filters(feature.alias):
                continue
            if feature.column == ref.time_column:
                role = "time"
            elif feature.column == ref.availability_column:
                role = "availability"
            else:
                continue
            if context.aggregate_filter is not None and not upper:
                continue
            # the bound holds on a path when its label side is on the spine on that path
            instances = frozenset(
                path
                for instance in resolver.paths[id(scope)]
                if all(resolver.on_spine(lab, instance) for lab in labels)
                for path in resolver.full_paths(feature, instance)
            )
            if not instances:
                continue
            # feature + f_offset OP label + l_offset + signed  ->  feature OP label + offset;
            # across several labels (UNION branches) the loosest reading counts
            pick = max if upper else min
            offset = pick(lab.offset for lab in labels) + signed - feature.offset
            label_trunc = min(lab.trunc for lab in labels)
            if upper and feature.trunc > label_trunc:
                offset += feature.trunc  # truncating only the feature side admits later facts
            bound = TimeBound(
                feature.column,
                role,
                normalized,
                offset,
                node.sql(spec.dialect),
                raw=feature.raw and all(lab.raw for lab in labels) and not shift,
                instances=instances,
                direct=not feature.chain,
                label_refs=frozenset(id(lab.ref) for lab in labels),
                slack_up=max(lab.up for lab in labels) + feature.down,
                inclusive=spec.boundary == "inclusive",
                post_window=context.kind == "qualify",
                node=node,
            )
            if context.aggregate_filter is not None:
                resolver.pending.append(
                    (ref, bound, context.aggregate_filter, scope, feature.alias)
                )
            else:
                ref.bounds.append(bound)
            recorded = True
        if recorded:
            return


def _record_latest_row(analysis: QueryAnalysis, resolver: _Resolver) -> None:
    """`col = (SELECT MAX(col) FROM same_table WHERE col < prediction_ts ...)` bounds `col`.

    The equality keeps only the row holding the subquery's maximum (or minimum), which is at or
    below every upper bound the subquery applies to its own rows. `col IN (SELECT MAX(col) ...
    GROUP BY entity)` keeps rows holding one of those per-group maxima: the same.
    """
    spec = analysis.spec
    for node in analysis.qualified.find_all(exp.EQ, exp.In):
        context = _row_filter(node)
        if context is None:
            continue
        scope = resolver.scope_of(node)
        if isinstance(node, exp.EQ):
            sides, grouped = [(node.left, node.right), (node.right, node.left)], False
        else:
            sides, grouped = [(node.this, node.args.get("query"))], True
        for feature_node, other in sides:
            if not isinstance(other, exp.Subquery) or not isinstance(other.this, exp.Select):
                continue
            inner = other.this
            inner_scope = resolver.by_expression.get(id(inner))
            if len(inner.selects) != 1 or inner_scope is None or id(inner_scope) in resolver.dead:
                continue
            if inner.args.get("group") and not grouped:
                continue
            peak = inner.selects[0].unalias()
            if not isinstance(peak, exp.Max | exp.Min):
                continue
            sub = resolver.resolve_time(peak.this, inner_scope, local=True)
            feature = resolver.resolve_time(feature_node, scope, local=True)
            if not sub or not feature or not feature.exact or feature.ref.time_column is None:
                continue
            ref = feature.ref
            if ref.table != sub.ref.table or feature.column != sub.column:
                continue
            if not context.filters(feature.alias):
                continue
            role = "time" if feature.column == ref.time_column else "availability"
            for bound in sub.ref.bounds:
                if not (bound.is_upper and bound.column == sub.column) or bound.post_window:
                    continue
                # written in the subquery itself (the table may be reached through a CTE)
                if not (bound.direct or resolver.scope_of(bound.node) is inner_scope):
                    continue
                instances = frozenset(
                    path
                    for instance in resolver.paths[id(scope)]
                    if any(p in bound.instances for p in resolver.full_paths(sub, instance))
                    for path in resolver.full_paths(feature, instance)
                )
                if instances:
                    ref.bounds.append(
                        TimeBound(
                            feature.column,
                            role,
                            bound.op,
                            bound.offset_seconds,
                            node.sql(spec.dialect),
                            raw=False,
                            instances=instances,
                            direct=not feature.chain,
                            label_refs=bound.label_refs,
                            slack_up=bound.slack_up,
                            inclusive=bound.inclusive,
                            node=node,
                        )
                    )


def _frame_stops_at_current_row(frame: exp.Expression | None, func: exp.Expression) -> bool:
    """Does the window frame end at the current row or before it (and so never look ahead)?

    Row numbering and LAG do not use a frame. For the rest the default frame with an ORDER BY
    ends at the current row's peers, and an explicit one must say CURRENT ROW or `n PRECEDING`.
    """
    if isinstance(func, exp.RowNumber | exp.Rank | exp.DenseRank) or (
        isinstance(func, exp.Lag) and not _lag_looks_ahead(func)
    ):
        return True
    if not isinstance(func, exp.AggFunc | exp.FirstValue | exp.LastValue | exp.NthValue):
        return False
    if frame is None:
        return True
    end, side = str(frame.args.get("end") or "").upper(), str(frame.args.get("end_side") or "")
    return end == "CURRENT ROW" or side.upper() == "PRECEDING"


def _timeline_key(
    order: list, scope: Scope, resolver: _Resolver
) -> tuple[str, list[Scope], list[Resolved], int] | None:
    """(alias, branches, what each branch's ordering column is, its position) for an ORDER BY
    over a UNION, or None.

    Each branch must give the first ordering key as a bare time column (a cast is fine): the
    spine's prediction_ts in one or more branches and a temporal table's own time or availability
    column in the others.
    """
    key = order[0].this
    if order[0].args.get("desc") or not isinstance(key, exp.Column) or not key.table:
        return None
    found = resolver._source(scope, key.table, local=True)
    if found is None or not isinstance(found[0], Scope) or found[1] is not scope:
        return None
    branches = _branch_scopes(found[0])
    position = _position(branches[0], key.name) if len(branches) > 1 else None
    if position is None:
        return None
    resolved = []
    for branch in branches:
        projection = branch.expression.selects[position]
        inner = projection.this if isinstance(projection, exp.Alias) else projection
        got = resolver.resolve_all(inner, branch, local=True)
        if not got or len(got) != 1 or not got[0].exact:
            return None
        resolved.append(got[0])
    return key.table, branches, resolved, position


def _label_sorts_first(order: list, alias: str, branches, is_label: list[bool], position) -> bool:
    """Is the second ORDER BY key a constant that puts a spine row ahead of every fact it ties
    with? Then a row's frame ends before the facts stamped exactly at its time."""
    if len(order) < 2:
        return False
    tie = order[1].this
    if not isinstance(tie, exp.Column) or tie.table.lower() != alias.lower():
        return False
    index = _position(branches[0], tie.name)
    if index is None:
        return False
    values = [_literal_value(b.expression.selects[index].unalias()) for b in branches]
    if any(v is None or isinstance(v, str) for v in values):
        return False
    if len({isinstance(v, bool) for v in values}) != 1:
        return False
    spine = [float(v) for v, lab in zip(values, is_label, strict=True) if lab]
    facts = [float(v) for v, lab in zip(values, is_label, strict=True) if not lab]
    if order[1].args.get("desc"):
        return min(spine) > max(facts)
    return max(spine) < min(facts)


def _record_timelines(analysis: QueryAnalysis, resolver: _Resolver) -> None:
    """A running window over one timeline of spine rows and facts bounds those facts.

    The usual way to write an as-of join without a join: UNION the spine (at prediction_ts) with
    a temporal table (at its time or ingestion column), order by that time, and let a window that
    ends at the current row carry the facts forward (`SUM(x) OVER (PARTITION BY k ORDER BY ts,
    ord ROWS UNBOUNDED PRECEDING)`, `last_value(x IGNORE NULLS) ...`). A spine row's frame then
    holds the facts stamped before it, and those stamped at the same instant only if they sort
    ahead of it: so the facts are bounded by prediction_ts, strictly when the second ordering key
    is a constant that sorts the spine first, otherwise inclusively (`<=`, which L5 reports).
    """
    spec = analysis.spec
    windows = list(analysis.qualified.find_all(exp.Window))
    earning = []
    for window in windows:
        parts = _window_parts(window)
        scope = resolver.scope_of(window)
        if (
            scope is None
            or not parts.order
            or not _frame_stops_at_current_row(parts.spec, parts.func)
        ):
            continue
        timeline = _timeline_key(parts.order, scope, resolver)
        if timeline is not None:
            earning.append((window, parts, scope, timeline))
    for window, parts, scope, timeline in earning:
        # The bound is earned by this window alone. It stands only while every window of the
        # SELECT is one that earns it and nothing downstream aggregates or windows over the rows
        # again: another window over the same rows (a whole-partition sum, a MAX over the carried
        # values) would read the facts, or the later spine rows' carried values, without it.
        if not _timeline_is_only_carried(scope, windows, [w for w, *_ in earning], resolver):
            continue
        alias, branches, resolved, position = timeline
        is_label = [g.ref.kind == "label" and g.column == spec.prediction_ts for g in resolved]
        facts = [g for g, lab in zip(resolved, is_label, strict=True) if not lab]
        if (
            not any(is_label)
            or not facts
            or any(g.ref.kind != "temporal" or g.column not in g.ref.time_columns for g in facts)
        ):
            continue
        strict = _label_sorts_first(parts.order, alias, branches, is_label, position)
        op = "<=" if not strict and spec.boundary != "inclusive" else "<"
        use = (id(scope), alias)
        spine_paths = [
            {p for p in g.ref.paths if p[-1:] == (use,) and (id(g.ref), p) in resolver.spine}
            for g, lab in zip(resolved, is_label, strict=True)
            if lab
        ]
        on_spine = set.intersection(*spine_paths) if spine_paths else set()
        labels = frozenset(id(g.ref) for g, lab in zip(resolved, is_label, strict=True) if lab)
        for fact in facts:
            paths = frozenset(p for p in fact.ref.paths if p in on_spine)
            if not paths:
                continue
            role = "availability" if fact.column == fact.ref.availability_column else "time"
            fact.ref.bounds.append(
                TimeBound(
                    fact.column,
                    role,
                    op,
                    0.0,
                    window.sql(spec.dialect),
                    raw=False,
                    instances=paths,
                    direct=False,
                    label_refs=labels,
                    inclusive=spec.boundary == "inclusive",
                    earned_by=window,
                )
            )


def _timeline_is_only_carried(
    scope: Scope, windows: list[exp.Window], earning: list[exp.Window], resolver: _Resolver
) -> bool:
    """Does nothing but the windows that earn a timeline bound read the rows of its SELECT?

    The SELECT reads only the timeline and has no other window, aggregate, GROUP BY, DISTINCT or
    HAVING; and no SELECT that reads it (however far down) has a window, an aggregate, a GROUP BY,
    a DISTINCT or a HAVING either: the carried values may only be projected, filtered and joined.
    """
    select = scope.expression
    if not isinstance(select, exp.Select) or len(scope.selected_sources) != 1:
        return False
    mine = [w for w in windows if resolver.scope_of(w) is scope]
    if any(not any(w is e for e in earning) for w in mine):
        return False
    for other in resolver.scope_by_id.values():
        if id(other) in resolver.dead:
            continue
        if other is not scope and not _reads_scope_of(other, scope, set()):
            continue
        body = other.expression
        if not isinstance(body, exp.Select):
            continue
        if any(body.args.get(k) for k in ("group", "distinct", "having")):
            return False
        for node in body.find_all(exp.AggFunc, exp.Window):
            if _innermost_select(node) is not body:
                continue
            if isinstance(node, exp.AggFunc) and node.find_ancestor(exp.Window) is not None:
                continue  # the function of a window, judged with the window
            if not (other is scope and any(node is w for w in mine)):
                return False
    return True


def _apply_filter_bounds(analysis: QueryAnalysis, resolver: _Resolver) -> None:
    """`agg(x) FILTER (WHERE ts < p)` bounds the table once every aggregate that reads it has one.

    One unfiltered aggregate (or any other use of the table's columns) still sums the whole
    history, so the FILTER bounds are dropped unless the query reads the table only through
    filtered aggregates, windows do not read it, and no output row depends on rows after p: the
    table is the null-supplying side of a LEFT JOIN, or the only source of a grouping-free query.
    """
    by_reader: dict[tuple[int, int, str], list] = defaultdict(list)
    for entry in resolver.pending:
        by_reader[(id(entry[0]), id(entry[3]), entry[4].lower())].append(entry)
    for entries in by_reader.values():
        ref, _, _, scope, alias = entries[0]
        covered = {id(f) for _, _, f, _, _ in entries}
        if scope is not None and _only_filtered_reads(scope, alias, covered):
            ref.bounds.extend(bound for _, bound, _, _, _ in entries)


def _only_filtered_reads(scope: Scope, alias: str, covered: set[int]) -> bool:
    """Does every consumer of the rows of the FROM item `alias` in the SELECT of `scope` sit
    behind a covered FILTER? (The item may be a CTE or derived table that reads the table.)

    A joined row that is not filtered out still counts: COUNT(1), SUM(1) or COUNT(spine column)
    grow with every later row of the entity, so each aggregate of the SELECT must carry a covered
    FILTER, unless it cannot see how many rows were joined (MAX / MIN, COUNT(DISTINCT ...)) and
    reads no column of the table. The table's columns may appear only inside covered FILTER
    aggregates and in its own join condition: a WHERE on it decides which spine rows survive.
    """
    select = scope.expression
    if not isinstance(select, exp.Select):
        return False
    scalar = alias not in {j.alias_or_name.lower() for j in select.args.get("joins") or []}
    for node in select.find_all(exp.AggFunc, exp.Window, exp.Column, exp.Anonymous):
        if _innermost_select(node) is not select:
            continue
        if isinstance(node, exp.Window):
            return False  # a window counts or orders the joined rows
        if isinstance(node, exp.Anonymous):
            if not _inside_covered_filter(node, covered):
                return False  # a function the analysis does not know may consume the joined rows
        elif isinstance(node, exp.AggFunc):
            if node.find_ancestor(exp.Window) is not None:
                continue
            if not _covered_filter(node, covered) and not _ignores_row_count(node, alias):
                return False
        elif (
            _reads_item(node, alias)
            and not _inside_covered_filter(node, covered)
            and not _in_own_join(node, select, alias)
            and not (scalar and _in_where(node, select))
        ):
            return False
    return _preserves_rows(alias, select)


def _covered_filter(agg: exp.Expression, covered: set[int]) -> bool:
    return (
        isinstance(agg.parent, exp.Filter) and agg.parent.this is agg and id(agg.parent) in covered
    )


def _inside_covered_filter(node: exp.Expression, covered: set[int]) -> bool:
    return any(isinstance(a, exp.Filter) and id(a) in covered for a in _ancestors(node, None))


def _ignores_row_count(agg: exp.Expression, alias: str) -> bool:
    """MAX / MIN, COUNT(DISTINCT x) and approx_count_distinct(x) give the same value however
    often a row is repeated."""
    insensitive = isinstance(agg, exp.Max | exp.Min | exp.ApproxDistinct) or (
        isinstance(agg, exp.Count) and isinstance(agg.this, exp.Distinct)
    )
    return insensitive and not any(_reads_item(c, alias) for c in agg.find_all(exp.Column))


def _in_own_join(node: exp.Expression, select: exp.Expression, alias: str) -> bool:
    return any(
        isinstance(a, exp.Join) and a.alias_or_name.lower() == alias
        for a in _ancestors(node, select)
    )


def _in_where(node: exp.Expression, select: exp.Expression) -> bool:
    return any(isinstance(a, exp.Where) for a in _ancestors(node, select))


def _reads_item(column: exp.Column, alias: str) -> bool:
    return column.table.lower() == alias


def _preserves_rows(alias: str, select: exp.Select) -> bool:
    """No output row exists or vanishes because of rows of the item stamped after prediction."""
    joins = select.args.get("joins") or []
    own = next((j for j in joins if j.alias_or_name.lower() == alias), None)
    if own is not None:
        return str(own.args.get("side") or "").upper() == "LEFT"
    return not select.args.get("group")  # a grouping-free aggregate always yields one row


def _global_scans(analysis: QueryAnalysis, resolver: _Resolver) -> None:
    """Uncorrelated `(SELECT MAX(label_ts) FROM labels)` used as a time-split cutoff.

    The scan yields one number computed from prediction times only and is compared with the
    spine's prediction_ts to choose which rows to train on; it is not a feature and not a
    per-row temporal join, so it needs no bound of its own.
    """
    spec = analysis.spec
    for ref in analysis.refs:
        if ref.kind != "label" or ref.on_spine:
            continue
        scope, select = ref.scope, ref.scope.expression
        if not scope.is_subquery or not isinstance(select, exp.Select) or scope.external_columns:
            continue
        shapes_ok = (
            len(scope.selected_sources) == 1
            and not any(select.args.get(k) for k in ("joins", "group", "having", "qualify"))
            and not select.find(exp.Window)
            and all(isinstance(p.unalias(), _CUTOFF_AGGREGATES) for p in select.selects)
            and sum(1 for _ in select.find_all(exp.Select)) == 1
        )
        if not shapes_ok:
            continue
        columns = list(select.find_all(exp.Column))
        reads_time_only = all(
            (got := resolver.resolve(c, scope)) is not None
            and got.ref is ref
            and got.column == spec.prediction_ts
            for c in columns
        )
        if reads_time_only and _used_as_spine_cutoff(select, resolver):
            ref.global_scan = True
            ref.time_column = None
            analysis.global_scans.append(ref)


def _anchored_at_maximum(aggregate: exp.Expression) -> bool:
    """MAX, or a quantile whose fraction is not a literal below 1 (1.0 is the maximum; a fraction
    we cannot read is assumed to be)."""
    if isinstance(aggregate, exp.Max):
        return True
    if isinstance(aggregate, exp.Quantile | exp.ApproxQuantile):
        fraction = aggregate.args.get("quantile")
    elif isinstance(aggregate, exp.PercentileCont | exp.PercentileDisc):
        fraction = aggregate.args.get("expression")
    else:
        return False  # MIN, median
    value = _constant(fraction) if fraction is not None else None
    return value is None or value >= 1


def _used_as_spine_cutoff(select: exp.Select, resolver: _Resolver) -> bool:
    """Is the scan compared, in a row filter, with nothing but spine prediction times?

    As an upper bound (`label_ts < cutoff`) it picks the training rows. As a lower bound anchored
    at the global maximum (`label_ts >= cutoff`, `BETWEEN cutoff AND ...`) it keeps only the
    latest rows, so which rows exist depends on later predictions: not accepted. Equality and
    IN are the same thing and are not accepted either.
    """
    subquery = select.parent
    if not isinstance(subquery, exp.Subquery):
        return False
    child, node = subquery, subquery.parent
    while isinstance(node, exp.Paren | exp.Add | exp.Sub | exp.Cast):
        if isinstance(node, exp.Sub) and node.this is not child:
            return False
        child, node = node, node.parent
    if isinstance(node, exp.LT | exp.LTE):
        lower = child is node.left
    elif isinstance(node, exp.GT | exp.GTE):
        lower = child is node.right
    elif isinstance(node, exp.Between):
        if child is node.this:
            return False
        lower = child is node.args["low"]
    else:
        return False
    if lower and any(_anchored_at_maximum(p.unalias()) for p in select.selects):
        return False
    context = _row_filter(node)
    if context is None:
        return False
    scope = resolver.scope_of(node)
    others = [
        c for c in node.find_all(exp.Column) if not any(a is subquery for a in _ancestors(c, node))
    ]
    if not others:
        return False
    return all(resolver.is_spine_time(column, scope) for column in others)


def _ancestors(node: exp.Expression, stop: exp.Expression | None) -> Iterator[exp.Expression]:
    parent = node.parent
    while parent is not None and parent is not stop:
        yield parent
        parent = parent.parent


def _label_uses(analysis: QueryAnalysis, resolver: _Resolver):
    """Label columns used to compute anything, filter a subquery, or partition a window.

    Passing a label through (possibly cast, parenthesized, or defaulted with a literal) and
    filtering or grouping the output rows by it are not uses: the output-column lineage check
    still catches a label that reaches a feature.
    """
    spec = analysis.spec
    uses = []
    for column in analysis.qualified.find_all(exp.Column):
        scope = resolver.scope_of(column)
        got = resolver.resolve(column, scope)
        if not got or got.ref.kind != "label" or got.column not in spec.label_columns:
            continue
        if _matured_use(got, scope, resolver, spec.label_known_after):
            continue  # labels that were final at prediction time are history, not the outcome
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
        if scope is not None and _filters_final_rows(column, got, scope, analysis, resolver):
            continue
        uses.append(
            LabelUse(got.ref, got.column, (column.parent or column).sql(spec.dialect), column)
        )
    return uses


def _matured_use(got: Resolved, scope: Scope | None, resolver: _Resolver, after) -> bool:
    """Is this use of a label column a read of rows whose labels were already final?"""
    if scope is None:
        return got.ref.matured(after)
    paths = [
        p for instance in resolver.paths[id(scope)] for p in resolver.full_paths(got, instance)
    ]
    return got.ref.matured_on(paths, after)


def _rows_are_output(scope: Scope, analysis: QueryAnalysis, resolver: _Resolver) -> bool:
    """Do the rows of `scope` become output rows (output scope, seed chain, or only the spine)?"""
    if id(scope) in analysis.seed_scopes:
        return True
    sources = [source for _, source in scope.selected_sources.values()]
    refs = [resolver.refs.get(id(s)) for s in sources if isinstance(s, exp.Table)]
    return (
        bool(sources)
        and len(refs) == len(sources)
        and all(r is not None and r.kind == "label" and r.on_spine for r in refs)
    )


def _filters_final_rows(column, got: Resolved, scope: Scope, analysis, resolver) -> bool:
    """Is a spine label `column` only grouping, ordering, or selecting the output rows?

    Grouping by it is harmless wherever it happens. Ordering, and filtering by a predicate that
    reads nothing but spine label columns, are accepted in scopes whose rows are the output
    rows. A predicate that mixes the label with a feature column selects feature rows by outcome.
    """
    ancestors = list(_ancestors(column, scope.expression))
    if any(isinstance(a, exp.Window | exp.Subquery) for a in ancestors) or not got.ref.on_spine:
        return False
    clause = next(
        (
            a
            for a in ancestors
            if isinstance(a, exp.Where | exp.Having | exp.Qualify | exp.Group | exp.Order)
        ),
        None,
    )
    if clause is None:
        return False
    if isinstance(clause, exp.Group):
        return True
    if not _rows_are_output(scope, analysis, resolver):
        return False
    if isinstance(clause, exp.Order):
        return True
    return _label_only_conjunct(column, clause, scope, resolver)


def _label_only_conjunct(column, clause, scope: Scope, resolver: _Resolver) -> bool:
    """Does the top-level AND-ed term holding `column` read only spine label columns?"""
    chain = [column]
    while chain[-1].parent is not clause and chain[-1].parent is not None:
        chain.append(chain[-1].parent)
    index = len(chain) - 1
    while index > 0 and isinstance(chain[index], exp.And | exp.Paren):
        index -= 1
    term = chain[index]
    if term.find(exp.Select) is not None:
        return False
    for other in term.find_all(exp.Column):
        got = resolver.resolve(other, scope)
        if got is None or got.ref.kind != "label" or not got.ref.on_spine:
            return False
    return True


_WHOLE_PARTITION = (exp.Ntile, exp.CumeDist, exp.PercentRank)


def _frame_looks_ahead(frame: exp.Expression | None) -> bool:
    if frame is None:
        return False
    return any(
        str(frame.args.get(side) or "").upper() == "FOLLOWING"
        for side in ("start_side", "end_side")
    )


def _lag_looks_ahead(func: exp.Expression) -> bool:
    """LAG with a negative offset reads later rows; an offset that is not a constant might."""
    offset = func.args.get("offset")
    if offset is None:
        return False
    value = _constant(offset)
    return value is None or value < 0


def _ordered_by_time(order: list, scope, ref: TableRef, resolver: _Resolver) -> bool:
    """Is the window ordered ascending by this table's own time (or the spine's prediction_ts)?

    Only then does a row's frame stop at its own time; any other order lets rows see ones
    stamped later. Ties on the leading key are the same instant, so later keys do not matter.
    Casting or truncating the key keeps it monotone, so it still counts.
    """
    if not order or order[0].args.get("desc"):
        return False
    got = resolver.resolve_all(order[0].this, scope)  # one per branch when a UNION feeds the key
    if not got:
        return False
    spine = all(
        g.ref.kind == "label" and g.column == resolver.spec.prediction_ts and g.ref.on_spine
        for g in got
    )
    # a merged timeline orders spine rows and several tables' facts by their own times
    timeline = any(g.ref is ref for g in got) and all(
        g.column in g.ref.time_columns and (g.ref.kind != "label" or g.ref.on_spine) for g in got
    )
    return spine or timeline


@dataclass(frozen=True)
class _WindowParts:
    """What a window is made of, with a named window (`OVER w`) read from its WINDOW clause."""

    func: exp.Expression
    partition_by: list
    order: list
    spec: exp.Expression | None


def _window_parts(window: exp.Window) -> _WindowParts:
    func = window.this
    while isinstance(func, exp.IgnoreNulls | exp.RespectNulls):
        func = func.this
    partition = list(window.args.get("partition_by") or [])
    order_arg, spec = window.args.get("order"), window.args.get("spec")
    name = window.alias
    if name and not (partition or order_arg or spec):
        select = window.find_ancestor(exp.Select)
        for definition in (select.args.get("windows") or []) if select is not None else []:
            if definition.name.lower() == name.lower():
                partition = list(definition.args.get("partition_by") or [])
                order_arg, spec = definition.args.get("order"), definition.args.get("spec")
                break
    order = list(order_arg.expressions) if order_arg else []
    return _WindowParts(func, partition, order, spec)


def _window_issues(analysis: QueryAnalysis, resolver: _Resolver):
    """Windows that let a row see later rows of a temporal table or of the label spine.

    Look-ahead: LEAD (or LAG with a negative or non-constant offset), a FOLLOWING frame, or any
    ORDER BY other than ascending time (descending, or by a value: the frame then reaches rows
    stamped later).
    Whole partition: an aggregate with no ORDER BY, or a distribution function (CUME_DIST,
    PERCENT_RANK, NTILE) whose value depends on the size of the partition.

    A window over a temporal table is safe when the table's rows are already strictly before
    prediction time (a bound in its own scope, or in one it reads from, runs before the window)
    and the window does not mix prediction times: it partitions by prediction_ts or a key of the
    prediction row, or the label rows are not in the window's scope at all (a correlated subquery
    runs once per outer row). It is also safe when it partitions by the table's own timestamp, or
    only derives a validity interval that is compared with prediction_ts.
    """
    issues = []
    for window in analysis.qualified.find_all(exp.Window):
        scope = resolver.scope_of(window)
        parts = _window_parts(window)
        func, order, frame = parts.func, parts.order, parts.spec
        base_ahead = (
            # Anonymous: a window function we do not know, assume the worst
            isinstance(func, exp.Anonymous | exp.Lead)
            or (isinstance(func, exp.Lag) and _lag_looks_ahead(func))
            or _frame_looks_ahead(frame)
        )
        base_whole = isinstance(func, _WHOLE_PARTITION) or (
            isinstance(func, exp.AggFunc) and not order and not base_ahead
        )
        targets: dict[int, TableRef] = {}
        for node in (*(item.this for item in order), *func.find_all(exp.Column)):
            for got in resolver.resolve_all(node, scope) or []:  # every branch of a UNION
                targets[id(got.ref)] = got.ref
        if not targets and scope is not None:  # COUNT(*): every source row counts
            targets = {id(r): r for r in analysis.refs if r.scope is scope}
        for ref in targets.values():
            if ref.time_column is None:
                continue  # static, outcome, or unclassified tables have no time to look past
            ahead = base_ahead or (
                bool(order) and not _ordered_by_time(order, scope, ref, resolver)
            )
            if not (ahead or base_whole):
                continue
            if _bounded_per_prediction(window, scope, ref, resolver):
                continue
            if _partitioned_by_time(window, scope, ref, resolver):
                continue
            if ahead and _validity_interval(window, scope, ref, analysis, resolver):
                continue
            kind = "lookahead" if ahead else "whole_partition"
            issues.append(WindowIssue(ref, kind, window.sql(analysis.spec.dialect), window))
    return issues


def _reaches(scope: Scope, target: TableRef, resolver: _Resolver, seen: set[int]) -> bool:
    if id(scope) in seen:
        return False
    seen.add(id(scope))
    for _, source in scope.selected_sources.values():
        if isinstance(source, exp.Table):
            if resolver.refs.get(id(source)) is target:
                return True
        elif isinstance(source, Scope) and _reaches(source, target, resolver, seen):
            return True
    return False


def _reads_scope_of(scope: Scope, other: Scope, seen: set[int]) -> bool:
    """Is `other` the scope itself, or one whose rows flow into it through FROM / JOIN items?"""
    if scope is other:
        return True
    if id(scope) in seen:
        return False
    seen.add(id(scope))
    inputs = [s for _, s in scope.selected_sources.values() if isinstance(s, Scope)]
    return any(
        _reads_scope_of(source, other, seen)
        for source in (*inputs, *(b for s in inputs for b in s.set_operation_scopes))
    )


def _cut_before(scope: Scope, ref: TableRef, resolver: _Resolver) -> bool:
    """Do the rows of `ref` reach the windows of `scope` already cut strictly before prediction
    time, on every path? The cutting predicate sits in `ref`'s own scope, in `scope` itself (a
    WHERE or ON runs before its SELECT's windows), or in a scope between them."""
    return ref.strictly_bounded_in_scope() or (
        bool(ref.temporal_paths)
        and all(
            any(
                path in b.instances
                and b.is_strict_past
                and not b.post_window
                and b.earned_by is None
                and (where := resolver.scope_of(b.node)) is not None
                and _reads_scope_of(scope, where, set())
                for b in ref.bounds
                if b.node is not None
            )
            for path in ref.temporal_paths
        )
    )


def _bounded_per_prediction(window, scope, ref: TableRef, resolver: _Resolver) -> bool:
    """The window only sees rows cut strictly before prediction time, one prediction at a time."""
    if not ref.temporal or scope is None or not _cut_before(scope, ref, resolver):
        return False
    for item in _window_parts(window).partition_by:
        if resolver.is_spine_time(item, scope, keyed=True):
            return True  # each partition is a single prediction
    by_id = {id(r): r for r in resolver.refs.values()}
    return any(
        all(not _reaches(scope, by_id[label], resolver, set()) for label in b.label_refs)
        for b in ref.bounds
        if b.is_strict_past
        and not b.post_window
        and (b.direct or (b.node is not None and _applies_in(b, scope, resolver)))
    )


def _applies_in(bound: TimeBound, scope: Scope, resolver: _Resolver) -> bool:
    where = resolver.scope_of(bound.node)
    return where is not None and _reads_scope_of(scope, where, set())


def _partitioned_by_time(window, scope, ref: TableRef, resolver: _Resolver) -> bool:
    """Each partition holds rows of one instant, so nothing in it lies in the future."""
    for item in _window_parts(window).partition_by:
        got = resolver.resolve_time(item, scope)
        if got and got.ref is ref and got.exact and got.column in ref.time_columns:
            return True
    return False


def _validity_interval(window, scope, ref, analysis, resolver) -> bool:
    """`LEAD(time) OVER (...) AS valid_to` used only to test validity against prediction_ts.

    The next version's timestamp is compared with prediction_ts (or tested for NULL, or
    defaulted with a constant first) and never reaches an output, and the table's rows are also
    bounded strictly before prediction time.
    """
    func, alias = window.this, window.parent
    if not isinstance(func, exp.Lead | exp.Lag):
        return False
    if not isinstance(alias, exp.Alias) or not isinstance(alias.parent, exp.Select):
        return False
    arg = resolver.resolve_time(func.this, scope, local=True)
    if not arg or arg.ref is not ref or not arg.exact or arg.column not in ref.time_columns:
        return False
    if not all(
        any(b.is_strict_past and not b.post_window and b.earned_by is None for b in g)
        for g in ref.bound_groups()
    ):
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


def _is_constant(node: exp.Expression) -> bool:
    return node.find(exp.Column) is None and node.find(exp.Select) is None


def _validity_use(column: exp.Column, resolver: _Resolver) -> bool:
    node, parent = column, column.parent
    while isinstance(parent, exp.Paren) or (
        isinstance(parent, exp.Coalesce)
        and parent.this is node
        and all(_is_constant(e) for e in parent.expressions)
    ):  # COALESCE(valid_to, <sentinel>) is the same open-ended interval
        node, parent = parent, parent.parent
    if not _in_filter(column):
        return False
    if isinstance(parent, exp.Is):
        return isinstance(parent.expression, exp.Null)
    if not isinstance(parent, _COMPARISONS):
        return False
    other = parent.right if parent.left is node else parent.left
    return resolver.prediction_labels(other, resolver.scope_of(column)) is not None


def _clock_uses(analysis: QueryAnalysis, resolver: _Resolver) -> list[ClockUse]:
    """Wall-clock functions anywhere but in a filter that only chooses spine rows.

    `WHERE l.label_ts < CURRENT_DATE - INTERVAL 30 DAY` picks which predictions to train on and
    changes nothing a feature sees. Anywhere else (a projection, a bound on a feature table) the
    value differs from run to run and from the prediction time the row stands for.
    """
    spec = analysis.spec
    uses = []
    for node in analysis.qualified.walk():
        is_clock = isinstance(node, _CLOCK_NODES) or (
            isinstance(node, exp.Anonymous)
            and (
                str(node.this).lower() in _CLOCK_FUNCTIONS
                # Spark: unix_timestamp() with no argument is the current time
                or (str(node.this).lower() == "unix_timestamp" and not node.expressions)
                # DuckDB / Postgres: age(ts) with one argument is the age as of today
                or (str(node.this).lower() == "age" and len(node.expressions) == 1)
            )
        )
        if not is_clock:
            continue
        scope = resolver.scope_of(node)
        if scope is None or _only_spine_filter(node, scope, analysis, resolver):
            continue
        uses.append(ClockUse(node.sql(spec.dialect), node))
    return uses


def _produces_spine_rows(scope: Scope, analysis: QueryAnalysis) -> bool:
    """Is `scope` the output SELECT or on the chain that feeds the spine, and does it read its
    label tables only as the spine (never also as history)?

    A subquery over a label reference merely equated to the spine computes a feature, and a label
    CTE read both as the spine and as history gives features that depend on the clock.
    """
    return id(scope) in analysis.seed_scopes and not any(
        ref.temporal for ref in analysis.refs if ref.scope is scope and ref.kind == "label"
    )


def _only_spine_filter(node, scope: Scope, analysis: QueryAnalysis, resolver: _Resolver) -> bool:
    """Is the clock call in a WHERE / HAVING of the SELECT that produces the spine rows?

    A join condition does not count: there the clock decides which feature values a row gets.
    """
    comparison = node.parent
    while comparison is not None and not isinstance(comparison, _TEST_NODES):
        if not isinstance(comparison, _TIME_ARITHMETIC):
            return False  # only plain arithmetic on the clock; anything else is a feature
        comparison = comparison.parent
    context = _row_filter(comparison) if comparison is not None else None
    if context is None or context.kind == "on" or not _produces_spine_rows(scope, analysis):
        return False
    columns = list(comparison.find_all(exp.Column))
    return bool(columns) and all(resolver.is_spine_time(c, scope) for c in columns)
