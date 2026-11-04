"""AST edits that turn a leaky training-set query into a point-in-time one.

Each function repairs one problem the static analysis reported, on the user's own parse tree, and
returns an `Edit` describing it (or None when the shape is not one it can repair safely). Nothing
here decides whether the result is right: `fixes.py` re-runs the validator on the edited query.

Shapes (see the point-in-time rewrite research):

- bounds go into the `ON` of the join (or the WHERE of a correlated subquery), never into the
  WHERE of an outer join, which would drop spine rows: `e.t < s.ts AND e.avail < s.ts`;
- an INNER JOIN that needed a bound becomes a LEFT JOIN so the spine keeps its rows (said in the
  caveats), and `COUNT(*)` becomes `COUNT(<feature key>)` so an unmatched row counts zero;
- the latest record before the prediction is a ROW_NUMBER over (entity, prediction time) ordered
  by event time then availability, with both bounds in the join; ASOF JOIN is used only for a
  table with a single time column, and only in DuckDB and Snowflake.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlglot import exp

from sqlguard.leakage.analysis import (
    QueryAnalysis,
    TableRef,
    TimeBound,
    WindowIssue,
)
from sqlguard.leakage.rules.boundary import loose_bounds
from sqlguard.leakage.rules.join_time import DAY, future_bounds

TAG = "fx"
ASOF_DIALECTS = ("duckdb", "snowflake")
_INNER = (None, "", "INNER")


@dataclass
class Edit:
    """One repair: what was done, and what it changes besides removing the leak."""

    rule: str
    location: str
    explanation: str
    caveats: list[str] = field(default_factory=list)


def tag_tree(tree: exp.Expression) -> dict[int, exp.Expression]:
    """Give every node an id that survives copies (the analysis works on a copy) and index them."""
    nodes: dict[int, exp.Expression] = {}
    used = [n.meta[TAG] for n in tree.walk() if TAG in n.meta]
    fresh = max(used, default=0) + 1
    for node in tree.walk():
        if TAG not in node.meta:
            node.meta[TAG] = fresh
            fresh += 1
        nodes[node.meta[TAG]] = node
    return nodes


def fresh(node: exp.Expression) -> exp.Expression:
    """A copy of `node` that does not share ids with the original."""
    clone = node.copy()
    for part in clone.walk():
        part.meta.pop(TAG, None)
    return clone


class Context:
    """A parsed query, its analysis, and the map from the analysis's nodes back to the parse."""

    def __init__(self, tree: exp.Expression, analysis: QueryAnalysis, dialect: str) -> None:
        self.tree = tree
        self.analysis = analysis
        self.spec = analysis.spec
        self.dialect = dialect
        self.nodes = tag_tree(tree)

    def orig(self, node: exp.Expression | None) -> exp.Expression | None:
        """The node of the user's parse that a node of the analyzed copy stands for."""
        if node is None:
            return None
        return self.nodes.get(node.meta.get(TAG, -1))

    def spine_column(self, ref: TableRef) -> exp.Column | None:
        """`<spine alias>.<prediction_ts>` as visible where `ref` is selected from."""
        return self.spine_in(ref.scope, ref.alias)

    def spine_in(self, scope, exclude: str | None) -> exp.Column | None:
        """The spine's prediction_ts as seen from `scope`, leaving out the FROM item `exclude`."""
        resolver, name = self.analysis.resolver, self.spec.prediction_ts
        if resolver is None:
            return None
        cur = scope
        while cur is not None:
            hits = []
            for alias, (node, _) in cur.selected_sources.items():
                if cur is scope and alias == exclude:
                    continue
                got = resolver.prediction_labels(exp.column(name, table=alias), cur)
                if got and all(g.ref.on_spine for g in got):
                    hits.append((alias, node))
            if hits:
                return exp.column(name, table=self._alias_of(*hits[0])) if len(hits) == 1 else None
            if not (cur.is_subquery or cur.is_udtf or cur.is_set_operation):
                return None  # a CTE or derived table cannot see the outer query's tables
            cur = cur.parent
        return None

    def scope_of(self, node: exp.Expression):
        """The analysis scope of a SELECT of the user's parse."""
        resolver = self.analysis.resolver
        if resolver is None:
            return None
        for part in self.analysis.qualified.walk():
            if part.meta.get(TAG) == node.meta.get(TAG):
                return resolver.by_expression.get(id(part))
        return None

    def _alias_of(self, alias: str, node: exp.Expression) -> str:
        """The name the user gave a FROM item (a derived table's node is its inner query)."""
        origin = self.orig(node)
        holder = origin if isinstance(origin, exp.Table) else getattr(origin, "parent", None)
        if isinstance(holder, exp.Table | exp.Subquery) and holder.alias_or_name:
            return holder.alias_or_name
        return alias


# --- building blocks -------------------------------------------------------------------------


def interval(seconds: float) -> exp.Interval:
    seconds = int(round(seconds))
    for unit, size in (("DAY", DAY), ("HOUR", 3600), ("MINUTE", 60)):
        if seconds % size == 0:
            return exp.Interval(this=exp.Literal.string(str(seconds // size)), unit=exp.var(unit))
    return exp.Interval(this=exp.Literal.string(str(seconds)), unit=exp.var("SECOND"))


def before(column: exp.Column, spine: exp.Column, seconds: float = 0.0, asof: bool = False):
    """`column < spine - seconds`; written `spine > column` inside an ASOF join condition."""
    limit = fresh(spine)
    if seconds:
        limit = exp.Sub(this=limit, expression=interval(seconds))
    if asof:
        condition = exp.GT(this=limit, expression=fresh(column))
        condition.meta[ASOF_MARK] = True
        return condition
    return exp.LT(this=fresh(column), expression=limit)


def since(column: exp.Column, spine: exp.Column, seconds: float) -> exp.Expression:
    """`column >= spine - seconds`."""
    return exp.GTE(
        this=fresh(column), expression=exp.Sub(this=fresh(spine), expression=interval(seconds))
    )


def conjoin(existing: exp.Expression | None, *new: exp.Expression) -> exp.Expression:
    out = existing
    for term in new:
        out = term if out is None else exp.And(this=_wrap(out), expression=_wrap(term))
    assert out is not None
    return out


def _wrap(node: exp.Expression) -> exp.Expression:
    return exp.Paren(this=node) if isinstance(node, exp.Or) else node


def _add_where(select: exp.Select, *preds: exp.Expression) -> None:
    where = select.args.get("where")
    if where is None:
        select.set("where", exp.Where(this=conjoin(None, *preds)))
    else:
        where.set("this", conjoin(where.this, *preds))


def _drop_conjunct(node: exp.Expression) -> None:
    parent = node.parent
    keep = parent.expression if parent.this is node else parent.this
    parent.replace(keep)


def _removable(node: exp.Expression) -> bool:
    return isinstance(node.parent, exp.And)


def _is_asof(join: exp.Join) -> bool:
    return str(join.args.get("method") or "").upper() == "ASOF"


def _asof_join(node: exp.Expression) -> exp.Join | None:
    """The ASOF join whose condition `node` is part of, if any."""
    join = node.find_ancestor(exp.Join)
    return join if join is not None and _is_asof(join) else None


def _inner(join: exp.Join) -> bool:
    return not join.args.get("side") and str(join.args.get("kind") or "").upper() in _INNER


def _alias(node: exp.Expression) -> str:
    return node.alias_or_name


def _select_of(table: exp.Expression) -> tuple[exp.Select, exp.Join | None] | None:
    """The SELECT a table is selected from in, and its JOIN (None for the FROM item)."""
    parent = table.parent
    if (
        isinstance(parent, exp.Join)
        and parent.this is table
        and isinstance(parent.parent, exp.Select)
    ):
        return parent.parent, parent
    if isinstance(parent, exp.From) and isinstance(parent.parent, exp.Select):
        return parent.parent, None
    return None


def _items_before(select: exp.Select, join: exp.Join | None) -> list[str]:
    """Aliases of the FROM items that precede `join` (all of them when it is None)."""
    names = []
    source = select.args.get("from_")
    if source is not None:
        names.append(_alias(source.this))
    for item in select.args.get("joins") or []:
        if item is join:
            break
        names.append(_alias(item.this))
    return names


# --- L2 / L3 / L4: missing bounds -------------------------------------------------------------


def add_bounds(ctx: Context, ref: TableRef, rule: str) -> Edit | None:
    """Bound the table's time (and availability) columns strictly before prediction time."""
    if ref.kind != "temporal" or len(ref.temporal_paths) != 1 or ref.node is None:
        return None
    table = ctx.orig(ref.node)
    placed = _select_of(table) if table is not None else None
    spine = ctx.spine_column(ref)
    if placed is None or spine is None or ctx.orig(ref.scope.expression) is not placed[0]:
        return None
    select, join = placed
    if join is not None and _is_asof(join):
        return None  # an ASOF join takes one inequality: two time columns cannot share it
    if join is not None and _inner(join) and not join.args.get("on") and not join.args.get("using"):
        join = None  # `FROM a, b WHERE ...`: the bound goes in the WHERE
    time, avail = ref.time_column, ref.availability_column
    group = ref.bound_groups()[0]
    wanted: list[str] = []
    if rule in ("L2", "L3") and not any(b.is_upper for b in group):
        wanted = [time] + ([avail] if avail else [])
    elif rule == "L4" and avail:
        wanted = [avail]
    if not wanted:
        return None
    alias = _alias(table)
    preds = [before(exp.column(c, table=alias), spine) for c in wanted]
    caveats: list[str] = []

    if join is not None:
        on = join.args.get("on")
        if on is None or not _visible(spine, select, join):
            return None
        join.set("on", conjoin(on, *preds))
        caveats += _keep_spine_rows(ctx, select, join, alias, ref)
    else:
        others = [j for j in select.args.get("joins") or [] if not _inner(j)]
        if others:
            return None
        _add_where(select, *preds)
        if spine.table in _items_before(select, None):
            caveats.append(
                f"the bound is in the WHERE of an inner join, so spine rows with no {ref.table} "
                "before the prediction time drop out of the result"
            )
    _order_by_availability(select, alias, time, avail)
    if _drop_having_bound(select, alias, ref, spine):
        caveats.append(
            "the HAVING MAX/MIN condition on the table's time was removed: it kept a spine row "
            "only when no history lay after the prediction time, which the join condition now "
            "guarantees; rows without any history now stay with NULL features"
        )
    names = " and ".join(wanted)
    return Edit(
        rule,
        ref.describe(),
        f"Bound {names} of {ref.describe()} strictly before {spec_col(spine)} in "
        f"{'the join condition' if join is not None else 'the WHERE'}, so each prediction "
        "only sees facts that had already landed.",
        caveats,
    )


def spec_col(column: exp.Column) -> str:
    return column.sql()


def _visible(spine: exp.Column, select: exp.Select, join: exp.Join | None) -> bool:
    """Is the spine alias a FROM item before `join` in this SELECT, or an enclosing query's?"""
    return spine.table in _items_before(select, join) or spine.table not in _items_before(
        select, None
    )


def _keep_spine_rows(
    ctx: Context, select: exp.Select, join: exp.Join, alias: str, ref: TableRef
) -> list[str]:
    """An INNER JOIN whose condition changed would drop spine rows with nothing to join: make it a
    LEFT JOIN, and keep `COUNT(*)` counting the joined side only."""
    if not _inner(join) or (join.args.get("method") and ctx.dialect == "snowflake"):
        return []
    join.set("kind", None)
    join.set("side", "LEFT")
    caveats = [
        "INNER JOIN became LEFT JOIN so every spine row is kept; rows with no "
        f"{ref.table} before the prediction time now carry NULL features (the original also "
        "dropped rows the join condition rejected)"
    ]
    key = _count_rows_of_joined_side(select, join, alias, ref)
    if key:
        caveats.append(f"COUNT(*) became COUNT({alias}.{key}) so a row without history counts 0")
    return caveats


def _drop_having_bound(select: exp.Select, alias: str, ref: TableRef, spine: exp.Column) -> bool:
    """`HAVING MAX(e.ts) < p` tried to bound the table after the join and dropped spine rows.

    Only a MAX of the table's time columns compared with the spine's prediction time (and
    constants) goes: that is the failed bound itself, not a condition on the data.
    """
    having = select.args.get("having")
    if having is None:
        return False
    terms, stack = [], [having.this]
    while stack:
        term = stack.pop()
        if isinstance(term, exp.And):
            stack += [term.this, term.expression]
        else:
            terms.append(term)
    drop = [t for t in terms if _is_max_bound(t, alias, ref, spine)]
    if not drop:
        return False
    if len(drop) == len(terms):
        select.set("having", None)
    else:
        for term in drop:
            _drop_conjunct(term)
    return True


def _is_max_bound(term: exp.Expression, alias: str, ref: TableRef, spine: exp.Column) -> bool:
    """`MAX(alias.t) < p [+- interval]`, in either order, with `<` or `<=`."""
    if isinstance(term, exp.LT | exp.LTE):
        peak, other = term.this, term.expression
    elif isinstance(term, exp.GT | exp.GTE):
        peak, other = term.expression, term.this
    else:
        return False
    if not (
        isinstance(peak, exp.Max)
        and isinstance(peak.this, exp.Column)
        and peak.this.table == alias
        and peak.this.name.lower() in {ref.time_column, ref.availability_column}
    ):
        return False
    return all(c.table == spine.table for c in other.find_all(exp.Column)) and not other.find(
        exp.Select
    )


def _count_rows_of_joined_side(
    select: exp.Select, join: exp.Join, alias: str, ref: TableRef
) -> str | None:
    """After INNER -> LEFT, `COUNT(*)` counts unmatched spine rows: count a column of the join."""
    stars = [
        c
        for c in select.find_all(exp.Count)
        if isinstance(c.this, exp.Star)
        and c.find_ancestor(exp.Select) is select
        and c.find_ancestor(exp.Window) is None
    ]
    if not stars:
        return None
    key = None
    for eq in join.args["on"].find_all(exp.EQ):
        for side in (eq.this, eq.expression):
            if isinstance(side, exp.Column) and side.table == alias:
                key = side.name
                break
        if key:
            break
    key = key or ref.time_column
    for star in stars:
        star.set("this", exp.column(key, table=alias))
    return key


def _order_by_availability(select: exp.Select, alias: str, time: str, avail: str | None) -> None:
    """A "latest record" ordered by event time gets availability as its tie-break."""
    if not avail:
        return
    orders = [w.args["order"] for w in select.find_all(exp.Window) if w.args.get("order")]
    if select.args.get("order"):
        orders.append(select.args["order"])
    for order in orders:
        keys = order.expressions
        first = keys[0] if keys else None
        if (
            first is not None
            and first.args.get("desc")
            and isinstance(first.this, exp.Column)
            and first.this.table == alias
            and first.this.name == time
            and not any(isinstance(k.this, exp.Column) and k.this.name == avail for k in keys)
        ):
            keys.append(exp.Ordered(this=exp.column(avail, table=alias), desc=True))


# --- L5: inclusive or shifted upper bound -----------------------------------------------------


def tighten(ctx: Context, ref: TableRef) -> Edit | None:
    """Make every loose upper bound a strict `<` prediction time."""
    if ref.kind != "temporal" or len(ref.temporal_paths) != 1:
        return None
    table = ctx.orig(ref.node)
    if table is None:
        return None
    nodes: dict[int, tuple[TimeBound, exp.Expression]] = {}
    for bound in loose_bounds(ref.bound_groups()[0]):
        node = ctx.orig(bound.node)
        if node is None or bound.slack_up:
            return None
        nodes[id(node)] = (bound, node)
    if not nodes:
        return None
    spine = ctx.spine_column(ref)
    plans = []
    for bound, node in nodes.values():
        if isinstance(node, exp.Between):
            column = exp.column(bound.column, table=_alias(table))
            if not (bound.direct and spine is not None and _is_column(node.this, column)):
                return None
            low = exp.GTE(this=fresh(node.this), expression=fresh(node.args["low"]))
            plans.append((node, conjoin(None, low, before(column, spine))))
        elif bound.raw and bound.op == "<=" and bound.offset_seconds == 0 and _flippable(node):
            plans.append((node, _strict(node)))  # `t <= p` -> `t < p`, wherever it is written
        elif bound.direct and spine is not None:
            column = exp.column(bound.column, table=_alias(table))
            plans.append((node, before(column, spine, asof=_asof_join(node) is not None)))
        else:
            return None
    joined = _own_inner_join(table, [n for n, _ in plans])
    for node, new in plans:
        _swap(node, new)
    return Edit(
        "L5",
        ref.describe(),
        "Make the upper bound strict (`<`, no offset) so facts stamped at the prediction time, "
        "or shifted past it by a timezone, are not read.",
        _keep_spine_rows(ctx, *joined, _alias(table), ref) if joined else [],
    )


def _own_inner_join(table: exp.Expression, edited: list[exp.Expression]):
    """(SELECT, JOIN) when an edited condition sits in the table's own join; call before editing."""
    placed = _select_of(table)
    if placed is None or placed[1] is None:
        return None
    join = placed[1]
    if any(n.find_ancestor(exp.Join) is join for n in edited):
        return placed
    return None


def _swap(old: exp.Expression, new: exp.Expression) -> None:
    """Replace a condition, keeping the mark of an ASOF inequality."""
    if old.meta.get(ASOF_MARK):
        new.meta[ASOF_MARK] = True
    old.replace(new)


def _is_column(node: exp.Expression, column: exp.Column) -> bool:
    return isinstance(node, exp.Column) and (node.table, node.name) == (column.table, column.name)


def _flippable(node: exp.Expression) -> bool:
    return isinstance(node, exp.LTE | exp.GTE)


def _strict(node: exp.Expression) -> exp.Expression:
    """`a <= b` -> `a < b`, `a >= b` -> `a > b` (same operands, same order)."""
    cls = exp.LT if isinstance(node, exp.LTE) else exp.GT
    return cls(this=fresh(node.this), expression=fresh(node.expression))


def _mirrored(node: exp.Expression) -> exp.Expression:
    """A lower bound on t (`t > p`, `p <= t`, ...) as the upper bound on t (`t < p`, `p > t`)."""
    cls = exp.LT if isinstance(node, exp.GT | exp.GTE) else exp.GT
    return cls(this=fresh(node.this), expression=fresh(node.expression))


# --- L1: bounds that reach past the prediction time -------------------------------------------


def mirror(ctx: Context, ref: TableRef) -> Edit | None:
    """Turn a window that looks forward from the prediction time into the same window behind it.

    "The first row after the prediction" becomes "the last row before it": a MIN / ARG_MIN over
    the mirrored column, and an ascending ranking on it, are turned around too.
    """
    if ref.kind != "temporal" or len(ref.temporal_paths) != 1:
        return None
    table = ctx.orig(ref.node)
    group = ref.bound_groups()[0]
    future = {id(b) for b in future_bounds(group)}
    if table is None or not future:
        return None
    spine = ctx.spine_column(ref)
    alias = _alias(table)
    # (nodes to replace, replacements): one for one when a bound is turned around where it
    # stands, else the first node becomes the whole new window and the others are dropped
    plans: list[tuple[list[exp.Expression], list[exp.Expression], bool]] = []
    columns = sorted({b.column for b in group if id(b) in future})
    for column in columns:
        mine = [b for b in group if b.column == column]
        lowers = [b for b in mine if not b.is_upper and b.offset_seconds >= 0]
        far = [b for b in mine if b.is_upper and b.offset_seconds >= DAY]
        ends = [b for b in mine if b.is_upper and b.offset_seconds > 0]
        removed = lowers + (ends if lowers else far)
        if any(b.slack_up or b.node is None for b in removed):
            return None
        nodes = list({id(n): n for n in (ctx.orig(b.node) for b in removed)}.values())
        if any(n is None or isinstance(n, exp.Between) for n in nodes):
            return None
        if lowers and not ends and all(b.raw and b.offset_seconds == 0 for b in lowers):
            # `t > p` alone: `t < p`, in place (also across a CTE, and in an ASOF condition)
            plans.append((nodes, [_mirrored(n) for n in nodes], True))
            continue
        if spine is None or any(not b.direct for b in removed):
            return None
        start = max((b.offset_seconds for b in lowers), default=0.0)
        end = max((b.offset_seconds for b in ends), default=None)
        col = exp.column(column, table=alias)
        asof = any(_asof_join(n) is not None for n in nodes)
        new = [before(col, spine, start, asof=asof)]
        if lowers and end is not None and end > start:
            new.insert(0, since(col, spine, end))
        if asof and len(new) > 1:
            return None
        plans.append((nodes, new, False))
    if any(not _removable(n) for nodes, _, in_place in plans if not in_place for n in nodes[1:]):
        return None
    joined = _own_inner_join(table, [n for nodes, _, _ in plans for n in nodes])
    for nodes, new, in_place in plans:
        if in_place:
            for old, replacement in zip(nodes, new, strict=True):
                _swap(old, replacement)
        else:
            nodes[0].replace(conjoin(None, *new))
            for extra in nodes[1:]:
                _drop_conjunct(extra)
    select = _select_of(table)
    flipped = _flip_extremum(select[0], alias, columns) if select else False
    caveats = ["the window is mirrored into the past: confirm that is the span you meant"]
    if joined:
        caveats += _keep_spine_rows(ctx, *joined, alias, ref)
    if flipped:
        caveats.append(
            "MIN / ascending order became MAX / descending: the row picked is the last before"
        )
    return Edit(
        "L1",
        ref.describe(),
        f"{ref.describe()} was read from the prediction time onwards; it is now read up to the "
        "prediction time, over the same span.",
        caveats,
    )


def _flip_extremum(select: exp.Select, alias: str, columns: list[str]) -> bool:
    """MIN(t) / ARG_MIN(x, t) / ascending order on a mirrored column -> the last row before."""
    changed = False

    def mine(node: exp.Expression | None) -> bool:
        return isinstance(node, exp.Column) and node.table == alias and node.name.lower() in columns

    for node in list(select.find_all(exp.Min, exp.ArgMin, exp.Ordered)):
        if node.find_ancestor(exp.Select) is not select:
            continue
        if isinstance(node, exp.Min) and mine(node.this):
            node.replace(exp.Max(this=node.this.copy()))
        elif isinstance(node, exp.ArgMin) and mine(node.expression):
            node.replace(exp.ArgMax(this=node.this.copy(), expression=node.expression.copy()))
        elif isinstance(node, exp.Ordered) and mine(node.this) and not node.args.get("desc"):
            node.set("desc", True)
        else:
            continue
        changed = True
    return changed


# --- L1 / L3: windows --------------------------------------------------------------------------


def fix_window(ctx: Context, issue: WindowIssue) -> Edit | None:
    """Windows that read later rows: the spine's own latest-prediction filter, a frame that
    reaches past the current row, a LEAD that is only an output feature."""
    window = ctx.orig(issue.node)
    if not isinstance(window, exp.Window) or issue.kind != "lookahead":
        return None
    where = issue.ref.describe()
    if issue.ref.kind == "label":
        return _drop_latest_prediction_filter(window, where)
    func = window.this
    while isinstance(func, exp.IgnoreNulls | exp.RespectNulls):
        func = func.this
    if isinstance(func, exp.Lead):
        return _drop_lead_feature(window, where)
    spec = window.args.get("spec")
    if spec is None or str(spec.args.get("kind") or "").upper() != "ROWS":
        return None
    keep_start = (
        str(spec.args.get("start_side") or "").upper() == "PRECEDING"
        or spec.args.get("start") == "UNBOUNDED"
    )
    window.set(
        "spec",
        exp.WindowSpec(
            kind="ROWS",
            start=spec.args.get("start") if keep_start else "UNBOUNDED",
            start_side="PRECEDING",
            end=exp.Literal.number(1),
            end_side="PRECEDING",
        ),
    )
    return Edit(
        "L1",
        where,
        "The frame reached rows after the current one; it now ends at the row before.",
        ["the current row is no longer part of its own frame"],
    )


def _drop_lead_feature(window: exp.Window, where: str) -> Edit | None:
    """`LEAD(x) OVER (...) AS next_x` as an output column of the final SELECT: remove the column.

    LEAD reads the next row. Replacing it with LAG would compute a different feature, so the only
    edit that keeps the query meaningful is to leave the column out; when the LEAD feeds anything
    else (a validity interval, a filter) it is explained instead.
    """
    alias = window.parent
    select = alias.parent if isinstance(alias, exp.Alias) else None
    if not isinstance(select, exp.Select) or select.parent is not None:
        return None
    name = alias.alias.lower()
    uses = [c for c in select.find_all(exp.Column) if c.name.lower() == name]
    if uses or len(select.selects) < 2:
        return None
    alias.pop()
    return Edit(
        "L1",
        where,
        f"LEAD reads the next row, which is later in time; the column {name!r} is removed.",
        [f"the output no longer has a column {name!r}; LAG would give the previous row instead"],
    )


def _drop_latest_prediction_filter(window: exp.Window, where: str) -> Edit | None:
    """`QUALIFY ROW_NUMBER() OVER (PARTITION BY entity ORDER BY prediction_ts DESC) = 1` keeps
    only the entity's last prediction: which rows exist then depends on later predictions."""
    eq = window.parent
    qualify = eq.parent if isinstance(eq, exp.EQ) else None
    if (
        not isinstance(window.this, exp.RowNumber)
        or not isinstance(qualify, exp.Qualify)
        or qualify.this is not eq
        or eq.expression != exp.Literal.number(1)
    ):
        return None
    select = qualify.parent
    select.set("qualify", None)
    return Edit(
        "L1",
        where,
        "Keeping only each entity's latest prediction decides which rows exist from predictions "
        "made later; the filter is removed so every prediction time is a training row.",
        ["the result has one row per prediction time instead of one per entity"],
    )


# --- ASOF --------------------------------------------------------------------------------------

ASOF_MARK = "asof"


def asof_condition(spine: exp.Column, column: exp.Column) -> exp.Expression:
    """The inequality of an ASOF join (`spine > column`: the newest row strictly before it).

    The tree always holds it inside ON, marked, whatever the dialect, because that is where the
    static checker reads time bounds. `render` writes it where the dialect wants it. Only for a
    table with one time column: the inequality is the only time condition an ASOF join can hold.
    """
    condition = exp.GT(this=fresh(spine), expression=fresh(column))
    condition.meta[ASOF_MARK] = True
    return condition


def fold_match_conditions(tree: exp.Expression) -> None:
    """Snowflake `ASOF JOIN t MATCH_CONDITION (c) ON eq` -> `ASOF JOIN t ON eq AND c`, in place."""
    for join in tree.find_all(exp.Join):
        condition = join.args.get("match_condition")
        if condition is not None:
            condition.meta[ASOF_MARK] = True
            join.set("match_condition", None)
            join.set("on", conjoin(join.args.get("on"), condition))


def render(tree: exp.Expression, dialect: str) -> str:
    """The SQL text of `tree`. DuckDB takes an ASOF inequality in ON, which sqlglot does not
    translate for Snowflake: there it is written by hand as `MATCH_CONDITION (...)`."""
    if dialect == "snowflake":
        tree = tree.copy()
        for join in tree.find_all(exp.Join):
            if str(join.args.get("method") or "").upper() != "ASOF" or join.args.get("on") is None:
                continue
            terms, stack = [], [join.args["on"]]
            while stack:
                term = stack.pop()
                if isinstance(term, exp.And):
                    stack += [term.expression, term.this]
                else:
                    terms.append(term)
            marked = [t for t in terms if t.meta.get(ASOF_MARK)]
            if len(marked) != 1:
                continue
            rest = [t for t in terms if t is not marked[0]]
            join.set("match_condition", marked[0])
            join.set("on", conjoin(None, *rest) if rest else None)
    return tree.sql(dialect=dialect, pretty=True)


# --- L2: "latest row" subquery read without the spine -----------------------------------------


@dataclass
class _Latest:
    """A derived table or single-use CTE that keeps the newest row per entity of one table."""

    select: exp.Select  # SELECT <columns> FROM table [WHERE ...] QUALIFY ROW_NUMBER() ... = 1
    table: exp.Table
    window: exp.Window
    holder: exp.Expression  # the Subquery, or the CTE
    use: exp.Expression  # the node in the outer FROM / JOIN that stands for it
    alias: str
    join: exp.Join


def _latest_row_source(ctx: Context, ref: TableRef) -> _Latest | None:
    table = ctx.orig(ref.node)
    select = ctx.orig(ref.scope.expression)
    if not isinstance(table, exp.Table) or not isinstance(select, exp.Select):
        return None
    joins, source = select.args.get("joins"), select.args.get("from_")
    plain = not any(
        select.args.get(k) for k in ("group", "having", "distinct", "limit", "order", "with_")
    )
    qualify = select.args.get("qualify")
    if joins or source is None or source.this is not table or not plain or qualify is None:
        return None
    eq = qualify.this
    window = eq.this if isinstance(eq, exp.EQ) else None
    if (
        not isinstance(window, exp.Window)
        or not isinstance(window.this, exp.RowNumber)
        or eq.expression != exp.Literal.number(1)
        or not all(
            isinstance(p.unalias(), exp.Column | exp.Star)
            and (not isinstance(p, exp.Alias) or p.alias == p.unalias().name)
            for p in select.selects
        )
    ):
        return None
    holder = select.parent
    if isinstance(holder, exp.Subquery):
        use = holder
        alias = holder.alias
    elif isinstance(holder, exp.CTE):
        name = holder.alias
        uses = [t for t in holder.root().find_all(exp.Table) if t.name == name and not t.db]
        if len(uses) != 1:
            return None
        use, alias = uses[0], uses[0].alias_or_name
    else:
        return None
    join = use.parent
    if not alias or not isinstance(join, exp.Join) or join.this is not use:
        return None
    if join.args.get("on") is None or not isinstance(join.parent, exp.Select):
        return None
    return _Latest(select, table, window, holder, use, alias, join)


def _requalify(node: exp.Expression, alias: str) -> exp.Expression:
    for column in node.find_all(exp.Column):
        column.set("table", exp.to_identifier(alias))
    return node


def inline_latest(ctx: Context, ref: TableRef, rule: str) -> Edit | None:
    """Replace "newest row per entity" computed without the spine by one computed per prediction.

    `JOIN (SELECT ... FROM t QUALIFY ROW_NUMBER() OVER (PARTITION BY k ORDER BY ts DESC) = 1) a
    ON a.k = s.k` takes the newest row of all time. Joined to the table itself with the bounds in
    ON, the newest row *before the prediction* is a ROW_NUMBER over (entity, prediction time), or
    an ASOF join when the table has a single time column and the dialect has one.
    """
    if rule not in ("L1", "L2") or ref.kind != "temporal" or len(ref.temporal_paths) != 1:
        return None
    latest = _latest_row_source(ctx, ref)
    if latest is None:
        return None
    outer = latest.join.parent
    scope = ctx.scope_of(outer)
    spine = ctx.spine_in(scope, latest.alias.lower()) if scope is not None else None
    time, avail = ref.time_column, ref.availability_column
    order = latest.window.args.get("order")
    first = order.expressions[0] if order is not None and order.expressions else None
    if (
        spine is None
        or first is None
        or not first.args.get("desc")
        or not isinstance(first.this, exp.Column)
        or first.this.name != time
        or not _visible(spine, outer, latest.join)
    ):
        return None
    if any(isinstance(p.unalias(), exp.Star) for p in outer.selects) or any(
        c.name == "*" for c in outer.find_all(exp.Column)
    ):
        return None  # `SELECT s.*` would list every column of the table, not the subquery's
    where = latest.select.args.get("where")
    if where is not None and where.find(exp.Select) is not None:
        return None
    alias = latest.alias
    table = fresh(latest.table)
    table.set("alias", exp.TableAlias(this=exp.to_identifier(alias)))
    extra = [_requalify(fresh(where.this), alias)] if where is not None else []
    join = latest.join
    caveats = [
        f"{ref.table} is read per prediction time, not once: rows with no version before the "
        "prediction time carry NULL features"
    ]
    keys = _equalities(join.args["on"], alias)
    partition = {
        p.name for p in latest.window.args.get("partition_by") or [] if isinstance(p, exp.Column)
    }
    simple = (
        keys is not None
        and bool(partition)
        and partition == {c.name for c in keys}
        and len(order.expressions) == 1
    )
    if _inner(join):
        join.set("kind", None)
        join.set("side", "LEFT")
        caveats.append(
            "INNER JOIN became LEFT JOIN so every spine row is kept; the original dropped "
            f"rows of entities without any {ref.table} row"
        )
    column = exp.column(time, table=alias)
    if avail is None and simple and ctx.dialect in ASOF_DIALECTS:
        on = conjoin(join.args["on"], *extra) if extra else join.args["on"]
        if not _only_equalities(on):
            return None
        join.set("this", table)
        join.set("method", "ASOF")
        if ctx.dialect == "snowflake":
            join.set("side", None)  # Snowflake's ASOF JOIN has no LEFT form
        join.set("on", conjoin(on, asof_condition(spine, column)))
        how = "ASOF JOIN on the one time column"
    else:
        if _blocks_ranking(outer):
            return None
        wanted = [time] + ([avail] if avail else [])
        preds = [before(exp.column(c, table=alias), spine) for c in wanted]
        join.set("this", table)
        join.set("on", conjoin(join.args["on"], *extra, *preds))
        ordering = [
            exp.Ordered(this=_requalify(fresh(k.this), alias), desc=bool(k.args.get("desc")))
            for k in order.expressions
        ]
        if avail and not any(
            isinstance(k.this, exp.Column) and k.this.name == avail for k in ordering
        ):
            ordering.append(exp.Ordered(this=exp.column(avail, table=alias), desc=True))
        identity = [exp.column(k, table=spine.table) for k in ctx.spec.entity_keys] + [fresh(spine)]
        ranked = exp.Window(
            this=exp.RowNumber(),
            partition_by=identity,
            order=exp.Order(expressions=ordering),
        )
        outer.set(
            "qualify", exp.Qualify(this=exp.EQ(this=ranked, expression=exp.Literal.number(1)))
        )
        caveats.append(
            f"rows are ranked per ({', '.join(ctx.spec.key_columns)}) of the spine: duplicate "
            "spine rows with the same key collapse into one"
        )
        how = "ROW_NUMBER over the entity and prediction time"
    if isinstance(latest.holder, exp.CTE):
        cte = latest.holder
        clause = cte.parent
        cte.pop()
        if not clause.expressions:
            clause.pop()
    return Edit(
        "L2",
        ref.describe(),
        f"The newest {ref.table} row was chosen over all history. It is now chosen per "
        f"prediction time among rows known before it ({how}).",
        caveats,
    )


def _equalities(on: exp.Expression, alias: str) -> list[exp.Column] | None:
    """The `alias` columns of an ON made only of column = column conjuncts, else None."""
    terms = [on]
    columns: list[exp.Column] = []
    while terms:
        term = terms.pop()
        if isinstance(term, exp.And):
            terms += [term.this, term.expression]
        elif (
            isinstance(term, exp.EQ)
            and isinstance(term.this, exp.Column)
            and isinstance(term.expression, exp.Column)
        ):
            columns += [c for c in (term.this, term.expression) if c.table == alias]
        else:
            return None
    return columns


def _only_equalities(on: exp.Expression) -> bool:
    return _equalities(on, "") is not None


def _blocks_ranking(select: exp.Select) -> bool:
    """A ROW_NUMBER filter needs a SELECT without grouping, DISTINCT or its own QUALIFY."""
    return any(select.args.get(k) for k in ("group", "having", "distinct", "qualify"))
