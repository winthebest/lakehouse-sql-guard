"""When may a fix be called `preserved`? Only when the whole query matches a small allowlist.

A bound on a table changes the values that come out of it; nothing else may change. That cannot be
shown by looking for the unsafe things people write (a WHERE on the aggregate, a LIMIT, an alias in
HAVING, an ordinal, a column alias list, `* REPLACE`, a struct field ...): there is always one more.
So the proof is the other way round. The edited query is qualified by sqlglot (aliases and
ordinals expanded, stars expanded, derived-table and CTE column lists pushed down); then it must
match exactly this, and anything the check does not recognise means "not shown":

1. Everything resolves: no `*` left in a projection, no column without a table, no column the
   qualifier could not place, and the bounded table appears once.
2. The bound sits in the ON of the bounded table's own LEFT JOIN (or the table is the only source
   of a correlated scalar subquery that is a projection item), with the table joined once.
3. In that SELECT there is no HAVING, QUALIFY, DISTINCT, LIMIT, OFFSET, FETCH or window function,
   and every reference to the bounded table's columns is the ON of its own join or an argument of
   an aggregate in the projection that is NULL (0 for a count of a column) for an unmatched row:
   SUM, AVG, MIN, MAX, COUNT of a column, COUNT(DISTINCT), STDDEV, VARIANCE, MEDIAN, QUANTILE,
   ARG_MIN, ARG_MAX, ANY_VALUE, over arguments that are NULL when the column is NULL (columns,
   literals, + - * / % ** and casts; no COALESCE, CASE, IS NULL, functions). A FILTER on such an
   aggregate counts as part of it: a condition that reads the table must be a comparison of
   NULL-strict expressions (possibly AND-ed with others, or negated), so it is never true for an
   unmatched row; one that can be (`IS NULL`, COALESCE, an OR with a spine condition) is not
   shown. WITHIN GROUP and an ORDER BY inside the aggregate are not accepted.
4. The outputs that carry those aggregates are followed up to the top. Each enclosing scope is
   either the top-level SELECT or a derived table / CTE read by plain SELECT wrappers: one source
   and nothing else (no WHERE, HAVING, QUALIFY, JOIN, ORDER BY, LIMIT, OFFSET, FETCH, DISTINCT,
   GROUP BY, window, subquery, set operation, column alias list), whose projection items are
   plain `column` or `column AS name` pass-throughs of the outputs they read.

Qualifying also means a name is only ever followed through a column that the qualifier resolved,
so a reference that is not made by plain name cannot slip past: it is either resolved into a
column by the qualifier or leaves an unresolved column, and either way the rule above applies.
"""

from __future__ import annotations

from sqlglot import exp
from sqlglot.optimizer.scope import Scope, traverse_scope

from sqlguard.core.lineage import Schema, qualify_statement
from sqlguard.leakage.rewrite import TAG, _clause_of, _null_strict, _reads, _unmatched_neutral

_SCALAR_WRAPPERS = (
    exp.Subquery,
    exp.Paren,
    exp.Alias,
    exp.Coalesce,
    exp.Add,
    exp.Sub,
    exp.Mul,
    exp.Div,
    exp.Cast,
)
_ROW_CLAUSES = ("having", "qualify", "distinct", "limit", "offset", "fetch")


class _Unproven(Exception):  # noqa: N818 - control flow, carries the reason
    pass


def _fail(reason: str) -> None:
    raise _Unproven(reason)


def unproven(
    tree: exp.Expression, tag: int, schema: Schema, dialect: str, local: bool = True
) -> str | None:
    """Why the edit cannot be shown to change only values, or None when it can.

    `tag` is the id (see `rewrite.tag_tree`) of the bounded table in the edited `tree`. With
    `local` False the join and aggregate checks of rule 3 are skipped and every output of the
    table's SELECT is followed upward (for edits that are not a bound on a join).
    """
    try:
        qualified = qualify_statement(tree, schema, dialect)
    except Exception as err:  # noqa: BLE001 - qualification failing is itself the answer
        return f"the query could not be qualified against the schema ({err})"
    try:
        _prove(qualified, tag, local)
    except _Unproven as why:
        return str(why)
    except Exception as err:  # noqa: BLE001 - anything unforeseen is "not shown"
        return f"the check could not be completed ({type(err).__name__})"
    return None


def _prove(root: exp.Expression, tag: int, local: bool) -> None:
    for select in root.find_all(exp.Select):
        for item in select.expressions:
            if isinstance(item.unalias(), exp.Star) or item.name == "*":
                _fail("a * in a projection could not be expanded")
    for column in root.find_all(exp.Column):
        if not column.table and not isinstance(column.this, exp.Star):
            _fail(f"{column.name} is not tied to a table (an alias, ordinal or unresolved name)")
    tables = [t for t in root.find_all(exp.Table) if t.meta.get(TAG) == tag]
    if len(tables) != 1:
        _fail("the bounded table is not found exactly once")
    table = tables[0]
    scopes = list(traverse_scope(root))
    home = next(
        (s for s in scopes for _, (node, _src) in s.selected_sources.items() if node is table), None
    )
    if home is None or not isinstance(home.expression, exp.Select):
        _fail("the bounded table is not selected from in a plain SELECT")
    alias = table.alias_or_name
    select = home.expression
    if local:
        carrier, tainted = _local(home, select, table, alias, scopes)
    else:
        carrier, tainted = home, {i.alias_or_name.lower() for i in select.expressions}
        if "" in tainted:
            _fail("an output of the SELECT has no name")
    _up(carrier, tainted, scopes, set())


def _local(home: Scope, select: exp.Select, table: exp.Table, alias: str, scopes: list[Scope]):
    parent = table.parent
    if isinstance(parent, exp.Join):
        _join_case(select, parent, table, alias)
        return home, _tainted(select, alias)
    if isinstance(parent, exp.From):
        return _scalar_case(home, select, alias, scopes)
    _fail("the bounded table is not the joined side of a join")
    raise AssertionError  # unreachable


def _limited(select: exp.Select) -> bool:
    return any(select.args.get(k) for k in _ROW_CLAUSES)


def _tainted(select: exp.Select, alias: str) -> set[str]:
    names = set()
    for item in select.expressions:
        if _reads(item, alias):
            name = item.alias_or_name.lower()
            if not name:
                _fail("an output that reads the bounded table has no name")
            names.add(name)
    return names


def _join_case(select: exp.Select, join: exp.Join, table: exp.Table, alias: str) -> None:
    if str(join.args.get("side") or "").upper() != "LEFT" or join.args.get("method"):
        _fail("the bounded table is not the joined side of a plain LEFT JOIN")
    if join.args.get("on") is None:
        _fail("the join has no ON condition")
    if _limited(select) or select.find(exp.Window) is not None:
        _fail("the SELECT has a HAVING, QUALIFY, DISTINCT, LIMIT or window function")
    if any(
        isinstance(j.this, exp.Table) and j.this.name == table.name
        for j in select.args.get("joins") or []
        if j is not join
    ):
        _fail("the table is joined more than once")
    own_on = {id(c) for c in join.args["on"].find_all(exp.Column)}
    aggregates = [
        a
        for a in select.find_all(exp.AggFunc)
        if a.find_ancestor(exp.Select) is select and _clause_of(a, select) == "expressions"
    ]
    allowed: set[int] = set()  # columns of the table that an aggregate may read
    for agg in aggregates:
        if agg.find_ancestor(exp.AggFunc) is not None:
            continue
        if not _unmatched_neutral(agg, alias):
            _fail(f"{agg.sql()} is not an aggregate known to be NULL for an unmatched row")
        if agg.find(exp.Select) is not None:
            _fail("a subquery inside an aggregate")
        allowed |= {id(c) for c in agg.find_all(exp.Column)}
        allowed |= _filter_columns(agg, alias)
    for node in select.find_all(exp.AggFunc):
        if node.find_ancestor(exp.Select) is select and _clause_of(node, select) != "expressions":
            _fail("an aggregate outside the projection")
    for column in select.find_all(exp.Column):
        if column.table != alias or id(column) in own_on or id(column) in allowed:
            continue
        _fail(f"{alias}.{column.name} is read outside an allowed aggregate argument or FILTER")
    for item in select.expressions:
        if _reads(item, alias) and item.find(exp.Select) is not None:
            _fail("an output built from the bounded table contains a subquery")


def _filter_columns(agg: exp.Expression, alias: str) -> set[int]:
    """The columns of the table in the FILTER of `agg`, once the filter is shown to be harmless.

    An aggregate that carries a FILTER over the bounded table must still give an unmatched row
    (all the table's columns NULL) the value of an aggregate over nothing, so the condition may
    not be true for it: a comparison of NULL-strict expressions is NULL there, `IS NULL` or
    COALESCE is not. A condition that does not read the table at all is the same with or without a
    match. Anything else wrapped around the aggregate that reads the table (WITHIN GROUP, an
    ORDER BY) is not accepted: its columns stay unaccounted for, and the caller fails.
    """
    found: set[int] = set()
    wrapper = agg.parent
    while isinstance(wrapper, exp.Filter | exp.WithinGroup | exp.IgnoreNulls | exp.RespectNulls):
        if isinstance(wrapper, exp.Filter):
            condition = wrapper.expression.this if wrapper.expression is not None else None
            if condition is not None and _reads(condition, alias):
                if not _not_true_when_unmatched(condition, alias):
                    _fail(
                        f"the FILTER ({condition.sql()}) can be true for an unmatched row, "
                        "which then gets a value of its own"
                    )
                found |= {id(c) for c in condition.find_all(exp.Column)}
        wrapper = wrapper.parent
    return found


_COMPARISONS = (exp.EQ, exp.NEQ, exp.LT, exp.LTE, exp.GT, exp.GTE, exp.Between, exp.In)


def _not_true_when_unmatched(node: exp.Expression, alias: str) -> bool:
    """Is `node` never TRUE when every column of the table is NULL?"""
    if isinstance(node, exp.Paren):
        return _not_true_when_unmatched(node.this, alias)
    if isinstance(node, exp.And):
        return _not_true_when_unmatched(node.this, alias) or _not_true_when_unmatched(
            node.expression, alias
        )
    if isinstance(node, exp.Or):
        return _not_true_when_unmatched(node.this, alias) and _not_true_when_unmatched(
            node.expression, alias
        )
    if isinstance(node, exp.Not):
        return _null_when_unmatched(node.this, alias)
    return _null_when_unmatched(node, alias)


def _null_when_unmatched(node: exp.Expression, alias: str) -> bool:
    """A comparison with an operand that is NULL when the table's columns are, and none that is
    not: NULL for an unmatched row, so a FILTER on it does not pass."""
    if isinstance(node, exp.Paren):
        return _null_when_unmatched(node.this, alias)
    if not isinstance(node, _COMPARISONS) or node.find(exp.Select) is not None:
        return False
    operands = [v for v in node.args.values() if isinstance(v, exp.Expression)]
    operands += [e for v in node.args.values() if isinstance(v, list) for e in v]
    reading = [o for o in operands if _reads(o, alias)]
    return bool(reading) and all(_null_strict(o, alias) for o in reading)


def _scalar_case(home: Scope, select: exp.Select, alias: str, scopes: list[Scope]):
    if select.args.get("joins") or len(select.expressions) != 1 or select.args.get("group"):
        _fail("the correlated subquery is not a single value read from one table")
    if select.find(exp.Window) is not None:
        _fail("the correlated subquery has a window function")
    item = select.expressions[0].unalias()
    if not (
        (isinstance(item, exp.Column) and item.table == alias)
        or (item.find(exp.AggFunc) is not None and not isinstance(item, exp.Column))
    ):
        _fail("the correlated subquery returns something other than the table's value")
    # the subquery must itself be a projection item of an enclosing SELECT
    node: exp.Expression = select
    while isinstance(node.parent, _SCALAR_WRAPPERS):
        node = node.parent
    outer = node.parent
    if not isinstance(outer, exp.Select) or node not in outer.expressions:
        _fail("the correlated subquery is not a plain item of an enclosing projection")
    if _limited(outer) or outer.find(exp.Window) is not None or outer.args.get("group"):
        _fail("the enclosing SELECT has a HAVING, QUALIFY, DISTINCT, LIMIT, GROUP BY or window")
    if _clause_of(select, outer) != "expressions":
        _fail("the correlated subquery is not in the projection")
    scope = next((s for s in scopes if s.expression is outer), None)
    if scope is None or node.alias_or_name == "":
        _fail("the enclosing projection item has no name")
    # nothing else may read this item by name: qualification leaves such a read as an unresolved
    # column, which rule 1 refuses
    return scope, {node.alias_or_name.lower()}


def _up(scope: Scope, tainted: set[str], scopes: list[Scope], seen: set[int]) -> None:
    """The tainted outputs of `scope` may only leave it through plain wrappers."""
    if id(scope) in seen:
        return
    seen.add(id(scope))
    if scope.is_root:
        return
    if scope.is_set_operation or isinstance(scope.expression.parent, exp.SetOperation):
        _fail("the values go through a set operation")
    if not (scope.is_cte or scope.is_derived_table):
        _fail("the values go into a subquery or other construct that is not a plain wrapper")
    holder = scope.expression.parent
    columns = None
    if isinstance(holder, exp.CTE | exp.Subquery):
        columns = holder.args.get("alias") and holder.args["alias"].args.get("columns")
    if columns:
        _fail("a column alias list renames the values")
    consumers = [
        (other, alias)
        for other in scopes
        for alias, (_node, source) in other.selected_sources.items()
        if source is scope
    ]
    stray = [
        1
        for other in scopes
        for source in [
            *other.lateral_sources.values(),
            *(other.sources[a] for a in other.semi_or_anti_join_tables if a in other.sources),
        ]
        if source is scope
    ]
    if stray:
        _fail("the values are read through a lateral or semi join")
    for other, alias in consumers:
        _wrapper(other, alias, tainted, scopes, seen)


def _wrapper(
    scope: Scope, alias: str, tainted: set[str], scopes: list[Scope], seen: set[int]
) -> None:
    select = scope.expression
    if not isinstance(select, exp.Select):
        _fail("the values are read by a set operation")
    for key in (
        "where",
        "group",
        "order",
        "joins",
        "laterals",
        *_ROW_CLAUSES,
    ):
        if select.args.get(key):
            _fail(f"a {key.strip('_').upper()} sits above the values")
    if select.find(exp.Window) is not None or any(
        not isinstance(q.parent, exp.From) for q in select.find_all(exp.Subquery)
    ):
        _fail("a window or subquery sits above the values")
    if len(scope.selected_sources) != 1 or scope.semi_or_anti_join_tables or scope.lateral_sources:
        _fail("the values are read next to another source")
    passed: set[str] = set()
    for item in select.expressions:
        inner = item.unalias()
        reads = [c for c in item.find_all(exp.Column) if c.table == alias]
        touching = [c for c in reads if c.name.lower() in tainted]
        if not touching:
            continue
        if not isinstance(inner, exp.Column) or inner.table != alias or len(reads) != 1:
            _fail(f"{item.sql()} is more than a plain pass-through of the values")
        name = item.alias_or_name.lower()
        if not name:
            _fail("a passed-through output has no name")
        passed.add(name)
    _up(scope, passed, scopes, seen)
