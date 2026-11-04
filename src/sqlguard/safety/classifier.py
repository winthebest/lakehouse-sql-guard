"""Static reversibility classification of agent actions. Nothing is executed.

Every statement is classified independently; the action's class is the most severe of its
statements. Anything the classifier does not recognise, cannot parse, or fails on is UNKNOWN,
which the policy denies (fail closed).
"""

from __future__ import annotations

import operator
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlglot import exp

from sqlguard.core.catalog import DEFAULT_NAMESPACE
from sqlguard.core.parsing import ParsedStatement, explained, parse
from sqlguard.safety.actions import (
    Action,
    DropTable,
    ExpireSnapshots,
    FastForward,
    MaintenanceAction,
    RemoveOrphanFiles,
    RollbackToSnapshot,
    SqlAction,
    UnsupportedCall,
    parse_spark_call,
    qualify_table_name,
)

if TYPE_CHECKING:
    from sqlguard.core.catalog import LakehouseCatalog


class ReversibilityClass(StrEnum):
    """How hard it is to undo an action. Ordered from least to most severe."""

    READ = "read"
    REVERSIBLE_WRITE = "reversible_write"
    RISKY_WRITE = "risky_write"
    SCHEMA_BREAKING = "schema_breaking"
    IRREVERSIBLE = "irreversible"
    OUT_OF_SCOPE = "out_of_scope"
    UNKNOWN = "unknown"

    @property
    def rank(self) -> int:
        return list(ReversibilityClass).index(self)

    @staticmethod
    def most_severe(classes: list[ReversibilityClass]) -> ReversibilityClass:
        """Return the most severe class; an empty list is UNKNOWN (fail closed)."""
        return max(classes, key=lambda c: c.rank) if classes else ReversibilityClass.UNKNOWN


# Verbs whose effect is bounded by how many existing rows they touch, so the affected-row
# threshold applies to them.
ROW_MODIFYING_VERBS = frozenset({"DELETE", "UPDATE", "MERGE"})


@dataclass(frozen=True)
class Impact:
    """Dry-run measurement of an action's blast radius; filled in by the branch executor."""

    rows_affected: int | None = None
    total_rows: int | None = None

    @property
    def pct(self) -> float | None:
        if self.rows_affected is None or not self.total_rows:
            return None
        return 100.0 * self.rows_affected / self.total_rows


@dataclass(frozen=True)
class StatementClass:
    """Classification of one statement (or one structured maintenance action)."""

    index: int
    verb: str
    klass: ReversibilityClass
    reasons: tuple[str, ...]
    tables: tuple[str, ...] = ()
    write_tables: tuple[str, ...] = ()
    has_where: bool | None = None
    sql: str = ""
    maintenance: MaintenanceAction | None = None


@dataclass(frozen=True)
class Classification:
    """Classification of a whole action: the strictest class plus per-statement detail."""

    klass: ReversibilityClass
    statements: tuple[StatementClass, ...]
    tables: tuple[str, ...]
    write_tables: tuple[str, ...]
    has_where: bool | None
    est_impact: Impact | None = None

    def with_impact(self, impact: Impact) -> Classification:
        return replace(self, est_impact=impact)


def classify(action: Action, catalog: LakehouseCatalog | None = None) -> Classification:
    """Classify `action`. `catalog` only supplies the default namespace for bare table names."""
    namespace = catalog.namespace if catalog is not None else DEFAULT_NAMESPACE
    if isinstance(action, SqlAction):
        statements = _classify_sql(action, namespace)
    elif isinstance(action, MaintenanceAction):
        statements = [_classify_maintenance(0, action, namespace, sql="")]
    else:
        statements = [_unknown(0, f"unsupported action type {type(action).__name__}")]
    return _aggregate(statements)


def _aggregate(statements: list[StatementClass]) -> Classification:
    where_flags = [s.has_where for s in statements if s.has_where is not None]
    return Classification(
        klass=ReversibilityClass.most_severe([s.klass for s in statements]),
        statements=tuple(statements),
        tables=_unique(t for s in statements for t in s.tables),
        write_tables=_unique(t for s in statements for t in s.write_tables),
        has_where=all(where_flags) if where_flags else None,
    )


def _headline(err: Exception) -> str:
    """First line of an error, bounded: parser errors append the offending SQL (and its values)."""
    lines = str(err).splitlines()
    return (lines[0] if lines else type(err).__name__)[:160]


def _unique(items) -> tuple[str, ...]:
    return tuple(dict.fromkeys(items))


def _unknown(index: int, reason: str, verb: str = "UNKNOWN", sql: str = "") -> StatementClass:
    return StatementClass(index, verb, ReversibilityClass.UNKNOWN, (reason,), sql=sql)


# -- SQL ---------------------------------------------------------------------------------------


def _classify_sql(action: SqlAction, namespace: str) -> list[StatementClass]:
    try:
        parsed = parse(action.sql, action.dialect)
    except Exception as err:  # fail closed: any tokenizer/parser failure must become UNKNOWN
        return [
            _unknown(0, f"SQL could not be parsed: {_headline(err)}", sql=action.sql.strip()[:200])
        ]
    results: list[StatementClass] = []
    for index, statement in enumerate(parsed):
        try:
            results.append(_classify_statement(index, statement, namespace, action.dialect))
        except Exception as err:  # fail closed on unexpected AST shapes
            results.append(
                _unknown(
                    index,
                    f"classifier error: {type(err).__name__}: {_headline(err)}",
                    sql=statement.sql,
                )
            )
    return results


_READ_ROOTS = (exp.Select, exp.SetOperation, exp.Subquery, exp.Describe, exp.Show)
_WRITE_NODES = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Merge,
    exp.Create,
    exp.Drop,
    exp.Alter,
    exp.TruncateTable,
)
_TRANSACTION_NODES = (exp.Commit, exp.Rollback, exp.Transaction)


@dataclass(frozen=True)
class _Effect:
    klass: ReversibilityClass
    verb: str
    reason: str
    has_where: bool | None = None


def _classify_statement(
    index: int, statement: ParsedStatement, namespace: str, dialect: str
) -> StatementClass:
    ast = statement.ast

    explain = explained(ast, dialect)
    if explain is not None:
        return _classify_explain(index, statement, explain, namespace, dialect)

    if isinstance(ast, exp.Command) and str(ast.this).upper() == "CALL":
        return _classify_call(index, ast, statement.sql, namespace)
    if isinstance(ast, _TRANSACTION_NODES):
        return _unknown(
            index,
            "transaction control is not allowed; the guard owns commit and rollback",
            verb=type(ast).__name__.upper(),
            sql=statement.sql,
        )

    effects = [_effect_for(ast)]
    # Data-modifying statements can hide inside a CTE or subquery of a read or another write.
    effects += [
        _effect_for(n)
        for n in ast.walk()
        if n is not ast and isinstance(n, _WRITE_NODES) and not _is_clause_of_parent(n)
    ]
    worst = max(effects, key=lambda e: e.klass.rank)

    tables, write_tables, table_function = _collect_tables(ast, namespace)
    reasons = [worst.reason]
    klass = worst.klass
    if table_function:
        klass = ReversibilityClass.UNKNOWN
        reasons = [f"table function {table_function} can read arbitrary files or external sources"]
    where_flags = [e.has_where for e in effects if e.has_where is not None]
    return StatementClass(
        index=index,
        verb=worst.verb,
        klass=klass,
        reasons=tuple(reasons),
        tables=tables,
        write_tables=write_tables,
        has_where=all(where_flags) if where_flags else None,
        sql=statement.sql,
    )


def _classify_explain(
    index: int, statement: ParsedStatement, explain, namespace: str, dialect: str
) -> StatementClass:
    """EXPLAIN of a read-only query is a read of the same tables; anything else is refused.

    EXPLAIN ANALYZE runs the statement, so explaining a write could change data; and a plain
    EXPLAIN of a write tells less than the guard's own branch rehearsal. Both stay UNKNOWN.
    """
    inner = _classify_statement(index, explain.statement, namespace, dialect)
    if inner.klass is not ReversibilityClass.READ or inner.verb == "EXPLAIN":
        return _unknown(
            index,
            f"EXPLAIN is only supported for read-only queries, not {inner.verb}"
            + (" (EXPLAIN ANALYZE runs the statement)" if explain.analyze else ""),
            verb="EXPLAIN",
            sql=statement.sql,
        )
    return replace(
        inner,
        verb="EXPLAIN",
        reasons=("query plan of a read-only query",),
        sql=statement.sql,
    )


def _is_clause_of_parent(node: exp.Expression) -> bool:
    """MERGE branches and ALTER actions reuse DML/DROP nodes that are not statements themselves."""
    return isinstance(node.parent, (exp.When, exp.Alter))


def _effect_for(node: exp.Expression) -> _Effect:
    unknown = ReversibilityClass.UNKNOWN
    if isinstance(node, (exp.Select, exp.SetOperation, exp.Subquery)):
        if node.args.get("into") is not None:
            return _Effect(
                ReversibilityClass.REVERSIBLE_WRITE, "SELECT", "SELECT INTO creates a table"
            )
        return _Effect(ReversibilityClass.READ, "SELECT", "read-only query")
    if isinstance(node, (exp.Describe, exp.Show)):
        return _Effect(ReversibilityClass.READ, type(node).__name__.upper(), "metadata read")

    if isinstance(node, exp.Insert):
        if node.args.get("overwrite"):
            return _Effect(
                ReversibilityClass.RISKY_WRITE,
                "INSERT",
                "INSERT OVERWRITE replaces existing rows of the target",
            )
        return _Effect(ReversibilityClass.REVERSIBLE_WRITE, "INSERT", "append rows")
    if isinstance(node, (exp.Delete, exp.Update)):
        verb = "DELETE" if isinstance(node, exp.Delete) else "UPDATE"
        where = node.args.get("where")
        if where is None:
            return _Effect(
                ReversibilityClass.RISKY_WRITE,
                verb,
                f"{verb} without WHERE clause touches every row",
                False,
            )
        if _truth(where.this) is True:
            return _Effect(
                ReversibilityClass.RISKY_WRITE,
                verb,
                f"{verb} WHERE clause is always true and touches every row",
                False,
            )
        return _Effect(
            ReversibilityClass.REVERSIBLE_WRITE, verb, f"{verb} restricted by a WHERE clause", True
        )
    if isinstance(node, exp.Merge):
        on = node.args.get("on")
        if on is None or _truth(on) is True:
            return _Effect(
                ReversibilityClass.RISKY_WRITE,
                "MERGE",
                "MERGE join condition is always true and matches every row",
                False,
            )
        return _Effect(ReversibilityClass.REVERSIBLE_WRITE, "MERGE", "MERGE with a join condition")
    if isinstance(node, exp.TruncateTable):
        return _Effect(
            ReversibilityClass.RISKY_WRITE, "TRUNCATE", "TRUNCATE removes every row", False
        )
    if isinstance(node, exp.Create):
        return _create_effect(node)
    if isinstance(node, exp.Drop):
        return _drop_effect(node)
    if isinstance(node, exp.Alter):
        return _alter_effect(node)
    return _Effect(unknown, type(node).__name__.upper(), f"unrecognised statement {node.key!r}")


def _create_effect(node: exp.Create) -> _Effect:
    kind = str(node.args.get("kind") or "").upper()
    if kind not in {"TABLE", "VIEW"}:
        return _Effect(
            ReversibilityClass.UNKNOWN, "CREATE", f"CREATE {kind or '?'} is not supported"
        )
    if node.args.get("replace"):
        return _Effect(
            ReversibilityClass.RISKY_WRITE,
            "CREATE",
            f"CREATE OR REPLACE {kind} overwrites the existing definition and contents",
        )
    return _Effect(ReversibilityClass.REVERSIBLE_WRITE, "CREATE", f"create {kind.lower()}")


def _drop_effect(node: exp.Drop) -> _Effect:
    kind = str(node.args.get("kind") or "").upper()
    if kind == "TABLE":
        if node.args.get("purge"):
            return _Effect(
                ReversibilityClass.IRREVERSIBLE, "DROP", "DROP TABLE PURGE deletes data files"
            )
        return _Effect(ReversibilityClass.SCHEMA_BREAKING, "DROP", "DROP TABLE removes the table")
    if kind == "VIEW":
        return _Effect(ReversibilityClass.SCHEMA_BREAKING, "DROP", "DROP VIEW removes the view")
    return _Effect(ReversibilityClass.UNKNOWN, "DROP", f"DROP {kind or '?'} is not supported")


def _alter_effect(node: exp.Alter) -> _Effect:
    if str(node.args.get("kind") or "").upper() != "TABLE":
        return _Effect(ReversibilityClass.UNKNOWN, "ALTER", "only ALTER TABLE is supported")
    actions = node.args.get("actions") or []
    if not actions:
        return _Effect(
            ReversibilityClass.UNKNOWN, "ALTER", "ALTER TABLE without recognised actions"
        )
    effects = [_alter_action_effect(a) for a in actions]
    return max(effects, key=lambda e: e.klass.rank)


def _alter_action_effect(action: exp.Expression) -> _Effect:
    breaking = ReversibilityClass.SCHEMA_BREAKING
    if isinstance(action, exp.ColumnDef):
        return _Effect(ReversibilityClass.REVERSIBLE_WRITE, "ALTER", "ADD COLUMN is additive")
    if isinstance(action, exp.Drop) and str(action.args.get("kind") or "").upper() == "COLUMN":
        return _Effect(breaking, "ALTER", "DROP COLUMN removes data and breaks readers")
    if isinstance(action, exp.RenameColumn):
        return _Effect(breaking, "ALTER", "RENAME COLUMN breaks readers")
    if isinstance(action, exp.AlterRename):
        return _Effect(breaking, "ALTER", "RENAME TABLE breaks readers")
    if isinstance(action, exp.AlterColumn):
        return _Effect(breaking, "ALTER", "ALTER COLUMN changes type or nullability")
    return _Effect(
        ReversibilityClass.UNKNOWN,
        "ALTER",
        f"ALTER action {type(action).__name__} is not recognised",
    )


# -- WHERE-clause constant folding -------------------------------------------------------------

_COMPARISONS = {
    exp.EQ: operator.eq,
    exp.NEQ: operator.ne,
    exp.LT: operator.lt,
    exp.LTE: operator.le,
    exp.GT: operator.gt,
    exp.GTE: operator.ge,
}


def _truth(node: exp.Expression) -> bool | None:
    """Constant-fold a boolean expression: True/False when decidable without data, else None.

    Only used to spot tautologies such as `1=1`, `TRUE`, `id = id` or `x LIKE '%'`. A real
    predicate (anything depending on column values) folds to None.
    """
    if isinstance(node, exp.Paren):
        return _truth(node.this)
    if isinstance(node, exp.Boolean):
        return bool(node.this)
    if isinstance(node, exp.Literal):
        return _number(node) != 0 if node.is_number else None
    if isinstance(node, exp.Not):
        inner = _truth(node.this)
        return None if inner is None else not inner
    if isinstance(node, exp.And):
        left, right = _truth(node.this), _truth(node.expression)
        if left is False or right is False:
            return False
        return True if left is True and right is True else None
    if isinstance(node, exp.Or):
        left, right = _truth(node.this), _truth(node.expression)
        if left is True or right is True:
            return True
        return False if left is False and right is False else None
    if isinstance(node, tuple(_COMPARISONS)):
        return _compare(node)
    if isinstance(node, exp.Like):
        pattern = node.expression
        if isinstance(pattern, exp.Literal) and pattern.is_string and set(pattern.this) == {"%"}:
            return True
    return None


def _number(node: exp.Literal) -> float:
    return float(node.this)


def _compare(node: exp.Expression) -> bool | None:
    compare = _COMPARISONS[type(node)]
    left, right = node.this, node.expression
    if isinstance(left, exp.Literal) and isinstance(right, exp.Literal):
        if left.is_number and right.is_number:
            return compare(_number(left), _number(right))
        if left.is_string and right.is_string:
            return compare(left.this, right.this)
        return None
    # `col = col` is true for non-NULL values; treat as a tautology (conservative direction).
    if isinstance(left, exp.Column) and left == right:
        return compare(0, 0)
    return None


# -- tables ------------------------------------------------------------------------------------


def _ident(part: exp.Expression | None) -> str:
    """Unquoted identifiers are case-insensitive (lowered); quoted ones keep their exact case."""
    if part is None:
        return ""
    name = part.name
    quoted = bool(part.args.get("quoted"))
    return name if quoted else name.lower()


def _table_name(table: exp.Table, namespace: str) -> str:
    catalog, db, name = (_ident(table.args.get(k)) for k in ("catalog", "db", "this"))
    parts = [p for p in (catalog, db or (namespace if not catalog else ""), name) if p]
    return ".".join(parts)


def _collect_tables(
    ast: exp.Expression, namespace: str
) -> tuple[tuple[str, ...], tuple[str, ...], str]:
    """Return (all tables, write targets, first table-function name or '')."""
    write_nodes = _write_target_nodes(ast)
    tables: list[str] = []
    writes: list[str] = []
    function_name = ""
    for table in ast.find_all(exp.Table):
        if not isinstance(table.this, exp.Identifier):
            function_name = function_name or type(table.this).__name__
            continue
        name = _table_name(table, namespace)
        is_write = any(table is node for node in write_nodes)
        if not is_write and is_cte_reference(table):
            continue
        tables.append(name)
        if is_write:
            writes.append(name)
    return _unique(tables), _unique(writes), function_name


def _write_target_nodes(ast: exp.Expression) -> list[exp.Table]:
    """Table nodes that are the destination of an INSERT/UPDATE/DELETE/MERGE/DDL anywhere."""
    targets: list[exp.Table] = []
    for node in ast.walk():
        if isinstance(node, (exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.Create, exp.Alter)):
            targets += list(node.this.find_all(exp.Table)) if node.this is not None else []
        elif isinstance(node, exp.Drop):
            targets += [t for t in node.args.get("tables") or [] if isinstance(t, exp.Table)]
        elif isinstance(node, exp.TruncateTable):
            targets += [t for t in node.expressions if isinstance(t, exp.Table)]
        elif isinstance(node, exp.Select) and node.args.get("into") is not None:
            targets += list(node.args["into"].find_all(exp.Table))
        elif isinstance(node, exp.AlterRename):
            targets += list(node.find_all(exp.Table))
    return targets


def is_cte_reference(table: exp.Table) -> bool:
    """True for an unqualified name that refers to a CTE visible at this point of the query.

    A CTE is visible in the body of the query that owns its WITH (and in the CTEs written after
    it in the same WITH), never outside that query: a name defined inside a subquery means
    nothing in the enclosing scope, where the same bare name is a real table. A CTE's own name
    inside its body is a real table too, except under WITH RECURSIVE.
    """
    if table.args.get("db") is not None or table.args.get("catalog") is not None:
        return False
    name = table.name.lower()
    if not name:
        return False
    child: exp.Expression = table
    for node in _ancestors(table):
        if isinstance(node, exp.With):
            siblings = node.expressions
            position = next((i for i, cte in enumerate(siblings) if cte is child), None)
            if position is not None:  # written inside one of this WITH's CTEs
                end = position + (1 if node.args.get("recursive") else 0)
                if any(cte.alias.lower() == name for cte in siblings[:end]):
                    return True
        else:
            owned = node.args.get("with_")
            if (
                isinstance(owned, exp.With)
                and child is not owned  # the query body, not the WITH itself
                and any(cte.alias.lower() == name for cte in owned.expressions)
            ):
                return True
        child = node
    return False


def _ancestors(node: exp.Expression):
    parent = node.parent
    while parent is not None:
        yield parent
        parent = parent.parent


# -- Spark CALL and structured maintenance -----------------------------------------------------


def _classify_call(index: int, node: exp.Command, sql: str, namespace: str) -> StatementClass:
    expression = node.args.get("expression")
    text = expression.name if expression is not None else ""
    try:
        action = parse_spark_call(text)
    except UnsupportedCall as err:
        return _unknown(index, f"CALL not understood: {_headline(err)}", verb="CALL", sql=sql)
    return _classify_maintenance(index, action, namespace, sql=sql)


def _classify_maintenance(
    index: int, action: MaintenanceAction, namespace: str, sql: str
) -> StatementClass:
    verb = _MAINTENANCE_VERBS.get(type(action), "")
    if not verb:
        return _unknown(index, f"unsupported maintenance action {type(action).__name__}", sql=sql)
    if not isinstance(action.table, str) or not action.table.strip():
        return _unknown(index, "maintenance action has no target table", verb=verb, sql=sql)
    table = qualify_table_name(action.table, namespace)
    klass, reason = _maintenance_class(action)
    return StatementClass(
        index=index,
        verb=verb,
        klass=klass,
        reasons=(reason,),
        tables=(table,),
        write_tables=(table,),
        sql=sql,
        maintenance=action,
    )


_MAINTENANCE_VERBS: dict[type[MaintenanceAction], str] = {
    ExpireSnapshots: "EXPIRE_SNAPSHOTS",
    RemoveOrphanFiles: "REMOVE_ORPHAN_FILES",
    RollbackToSnapshot: "ROLLBACK",
    FastForward: "FAST_FORWARD",
    DropTable: "DROP_TABLE",
}


def _maintenance_class(action: MaintenanceAction) -> tuple[ReversibilityClass, str]:
    if isinstance(action, ExpireSnapshots):
        return (
            ReversibilityClass.IRREVERSIBLE,
            "expire_snapshots permanently removes history needed for rollback and time travel",
        )
    if isinstance(action, RemoveOrphanFiles):
        return ReversibilityClass.IRREVERSIBLE, "remove_orphan_files physically deletes data files"
    if isinstance(action, DropTable):
        if action.purge:
            return ReversibilityClass.IRREVERSIBLE, "drop_table with purge deletes data files"
        return ReversibilityClass.SCHEMA_BREAKING, "drop_table removes the table from the catalog"
    if isinstance(action, RollbackToSnapshot):
        return (
            ReversibilityClass.RISKY_WRITE,
            "moves the current-state pointer without validation; reversible only while the "
            "snapshots are retained",
        )
    return (
        ReversibilityClass.RISKY_WRITE,
        "fast_forward publishes a branch to main without the guard's validation",
    )
