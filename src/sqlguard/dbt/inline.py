"""Turn a training model into one query by inlining the models it is built from.

A leak is usually written in an intermediate model (a lifetime aggregate in `int_*`), not in the
training model that joins it, and the rules need the whole query to judge a bound against the
prediction time. `inline` replaces every reference to an upstream model with a CTE holding that
model's compiled SQL, recursively, until it reaches a table the spec can classify:

- sources, seeds and snapshots;
- models declaring `role: label_spine` or `role: static`;
- models declaring `time_column` (a temporal table whose time semantics the author vouches for).

Those stay in the query as physical tables. Ephemeral models are already inlined by dbt. Every AST
node is tagged with the model its SQL came from (`OWNER`), so a finding can be traced back to the
file that holds the leak.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlglot import exp

from sqlguard.core.parsing import parse_single
from sqlguard.dbt.manifest import Manifest, Node

OWNER = "dbt_node"  # meta key: unique_id of the model whose compiled SQL a node came from
PREFIX = "dbt_model__"  # CTE names of inlined models
MAX_DEPTH = 64  # models between the training model and the deepest one inlined
STOP_KINDS = ("source", "seed", "snapshot")
STOP_ROLES = ("label_spine", "static")


class InlineError(ValueError):
    """The upstream models cannot be turned into one query."""


@dataclass(frozen=True)
class Inlined:
    root: Node
    tree: exp.Expression  # the query; nodes carry `OWNER` meta
    sql: str
    boundaries: dict[str, Node]  # unique_id -> physical tables the query reads
    models: dict[str, Node]  # unique_id -> models whose SQL is part of the query (root first)
    queries: dict[str, exp.Expression]  # unique_id -> that model's own query inside `tree`


def is_boundary(node: Node) -> bool:
    """Is `node` read as a physical table (never inlined)?"""
    return node.kind in STOP_KINDS or node.role in STOP_ROLES or "time_column" in node.leakage


def relation_key(relation_name: str, dialect: str) -> tuple[str, ...]:
    """Lowercase (catalog, schema, name) parts of a dbt relation name, as SQL spells them."""
    table = exp.to_table(relation_name, dialect=dialect)
    return tuple(p.lower() for p in (table.catalog, table.db, table.name) if p)


def _table_key(table: exp.Table) -> tuple[str, ...]:
    return tuple(p.lower() for p in (table.catalog, table.db, table.name) if p)


def _parse(node: Node, root: Node, dialect: str) -> exp.Expression:
    if not node.compiled_code:
        raise InlineError(
            f"{node.unique_id} has no compiled_code ({node.original_file_path}): compile it "
            f"with `dbt compile --select +{root.name}`"
        )
    try:
        tree = parse_single(node.compiled_code, dialect)
    except ValueError as err:
        raise InlineError(f"{node.unique_id}: cannot parse compiled SQL: {err}") from None
    if not isinstance(tree, exp.Query):
        raise InlineError(f"{node.unique_id}: compiled SQL is not a SELECT query")
    if any(c.alias_or_name.lower().startswith(PREFIX) for c in tree.find_all(exp.CTE)):
        raise InlineError(f"{node.unique_id}: a CTE name starts with the reserved {PREFIX!r}")
    for part in tree.walk():
        part.meta[OWNER] = node.unique_id
    return tree


def inline(manifest: Manifest, root: Node, max_depth: int = MAX_DEPTH) -> Inlined:
    """The query of `root` with its upstream models inlined as CTEs."""
    dialect = manifest.dialect
    expanded: dict[str, str] = {}  # unique_id -> CTE name, in dependency order
    bodies: list[tuple[str, exp.Expression]] = []
    boundaries: dict[str, Node] = {}
    models: dict[str, Node] = {root.unique_id: root}
    queries: dict[str, exp.Expression] = {}

    def cte_name(node: Node) -> str:
        taken = {name.lower() for name in expanded.values()}
        name, n = f"{PREFIX}{node.name}", 1
        while name.lower() in taken:
            n += 1
            name = f"{PREFIX}{node.name}_{n}"
        return name

    def expand(node: Node, path: tuple[str, ...]) -> exp.Expression:
        tree = _parse(node, root, dialect)
        # A dependency missing from the manifest, or without a relation (an ephemeral model dbt
        # already inlined), never matches a table; one that does stays a physical table the
        # spec must classify.
        upstream = {
            relation_key(dep.relation_name, dialect): dep
            for dep in (manifest.nodes.get(d) for d in node.depends_on)
            if dep is not None and dep.relation_name
        }
        for table in list(tree.find_all(exp.Table)):
            dep = upstream.get(_table_key(table))
            if dep is None:
                continue  # a physical table not built from a dbt node: the spec must classify it
            if is_boundary(dep):
                boundaries[dep.unique_id] = dep
                continue
            name = use(dep, (*path, node.unique_id))
            used = table.name
            if not table.alias:
                table.set("alias", exp.TableAlias(this=exp.to_identifier(used)))
            table.set("this", exp.to_identifier(name))
            table.set("db", None)
            table.set("catalog", None)
        return tree

    def use(dep: Node, path: tuple[str, ...]) -> str:
        if dep.unique_id in path:
            loop = (*path[path.index(dep.unique_id) :], dep.unique_id)
            cycle = " -> ".join(p.rsplit(".", 1)[-1] for p in loop)
            raise InlineError(f"models depend on each other in a cycle: {cycle}")
        if len(path) >= max_depth:
            raise InlineError(f"{root.unique_id} is built from more than {max_depth} nested models")
        if dep.unique_id in expanded:
            return expanded[dep.unique_id]
        body = expand(dep, path)
        name = cte_name(dep)
        expanded[dep.unique_id] = name
        models[dep.unique_id] = dep
        queries[dep.unique_id] = body
        bodies.append((name, body))
        return name

    tree = expand(root, ())
    queries[root.unique_id] = tree
    if bodies:
        ctes = [
            exp.CTE(this=body, alias=exp.TableAlias(this=exp.to_identifier(name)))
            for name, body in bodies
        ]
        own = tree.args.get("with_")
        if own is not None:
            ctes += own.expressions
            own.set("expressions", ctes)
        else:
            tree.set("with_", exp.With(expressions=ctes))
    sql = tree.sql(dialect=dialect, pretty=True)
    return Inlined(root, tree, sql, boundaries, models, queries)
