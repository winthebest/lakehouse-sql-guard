"""Near-duplicate and shape checks for a held-out template set.

A held-out split is only held out if its templates are not copies of what the rules were
developed against, and a set is only as big as its distinct templates. Every case of the candidate
templates is compared with every case of the reference splits (by default the splits
`splits.yaml` marks as seen), and the candidate set is compared with itself:

- `id`: the same template id;
- `sql`: the same SQL after parsing (comments, whitespace, keyword case and quoting do not count);
- `ast`: the same query after a canonical form that ignores how it is written: aliases and CTE
  names are renumbered; qualified relation names are reduced to the catalog table; literals are
  masked; `AND`/`OR`/`+`/`*` operands, comparison direction (`a > b` is `b < a`), `SELECT` items,
  `GROUP BY` items and inner-join order are put in a fixed order; `BETWEEN` is expanded; `INTERVAL`
  spellings (`'7 days'`, `7 DAY`, `to_days(7)`) are one; `GROUP BY ALL` and ordinals are expanded;
  a `CAST`/`::` to a type that is not a date or time and a literal `COALESCE` default are dropped;
  `JOIN` kinds and `RIGHT` joins with swapped tables are one; a CTE or subquery that only passes
  a table through (`SELECT * FROM t`) is inlined;
- `similar`: not equal, but the Jaccard similarity of the canonical form's parts (predicates,
  functions, joins, tables, select items) reaches `SIMILAR_AT`. An extra feature column or one more
  predicate still lands here. The score is reported so a person decides.

What it still misses: a rewrite that needs semantics (a join turned into `EXISTS`, a window turned
into a self-join), reordering of the steps of a CTE chain, and an alias reused for different tables
in different scopes (numbered once). Within the candidate set, near-copies of the same label are
collapsed into one template before the quotas are counted, so padding does not help; a pair with
different labels (a clean twin of a leaky shape) is never collapsed.

`shape_counts` checks a candidate set against the author brief's quotas (clean and leaky templates
per type, share of dbt-style shapes), counting distinct templates.

Usage: python -m bench.leakage.duplicates --candidate-dir DIR [--against dev dev_v1b]
       python -m bench.leakage.duplicates --candidate holdout_v2 --counts
"""

from __future__ import annotations

import argparse
import hashlib
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

import yaml
from sqlglot import exp, parse_one
from sqlglot.errors import SqlglotError

from bench.leakage.cases import LEAK_TYPES, TEMPLATES_DIR, Template, load_template, render
from bench.leakage.splits import load_splits, seen_splits

MIN_CLEAN = 25
MIN_LEAKY_PER_TYPE = 8
MIN_DBT_SHARE = 0.30
DBT_CTE_DEPTH = 3  # a query with at least this many CTEs is dbt-style
SIMILAR_AT = 0.7  # Jaccard similarity of canonical parts from which two queries are reported
_TIME_FUNCTIONS = {
    "to_years": "YEAR",
    "to_months": "MONTH",
    "to_weeks": "WEEK",
    "to_days": "DAY",
    "to_hours": "HOUR",
    "to_minutes": "MINUTE",
    "to_seconds": "SECOND",
}


@dataclass(frozen=True)
class Duplicate:
    kind: str  # "id" | "sql" | "ast" | "similar"
    candidate: str  # case id
    reference: str  # case id
    reference_split: str
    score: float | None = None  # Jaccard similarity, for "similar"

    def __str__(self) -> str:
        score = f" ({self.score:.2f})" if self.score is not None else ""
        return (
            f"{self.kind}: {self.candidate} is a near-duplicate of "
            f"{self.reference_split}/{self.reference}{score}"
        )


def _parse(sql: str, dialect: str) -> exp.Expression:
    try:
        tree = parse_one(sql, read=dialect)
    except SqlglotError as err:
        raise ValueError(f"cannot parse: {err}") from err
    for node in list(tree.walk()):
        node.comments = None  # comments never count
    return tree


def _lower_identifiers(tree: exp.Expression) -> None:
    for ident in tree.find_all(exp.Identifier):
        ident.set("this", ident.name.lower())
        ident.set("quoted", False)


def normalized_sql(sql: str, dialect: str = "duckdb") -> str:
    """`sql` as one canonical string: parsed, comment-free, identifiers lower-cased."""
    tree = _parse(sql, dialect)
    _lower_identifiers(tree)
    return tree.sql(dialect=dialect)


def _table_target(table: exp.Table, relations: Mapping[str, str]) -> str:
    parts = [p.lower() for p in (table.catalog, table.db, table.name) if p]
    mapped = relations.get(".".join(parts))
    return (mapped or parts[-1]).rsplit(".", 1)[-1]


def _unit(name: str) -> str:
    name = name.upper()
    return name[:-1] if len(name) > 3 and name.endswith("S") else name


def _interval(unit: str) -> exp.Interval:
    return exp.Interval(this=exp.Literal.string("?"), unit=exp.Var(this=_unit(unit)))


_TIME_TYPES = {exp.DType.DATE, exp.DType.TIMESTAMP, exp.DType.TIMESTAMPTZ, exp.DType.TIME}


def _casts_time(node: exp.Cast | exp.TryCast) -> bool:
    """A cast to a date or time type changes what a time comparison means (a day, not an
    instant), so it is part of the query's shape; any other cast is a wrapper."""
    target = node.args.get("to")
    return target is not None and target.this in _TIME_TYPES


def _rewrite(node: exp.Expression) -> exp.Expression:
    """One node's value-level normalisations (the order of evaluation does not matter)."""
    if isinstance(node, exp.Between):
        low = exp.GTE(this=node.this.copy(), expression=node.args["low"].copy())
        high = exp.LTE(this=node.this.copy(), expression=node.args["high"].copy())
        return exp.And(this=low, expression=high)
    if isinstance(node, exp.Cast | exp.TryCast) and not _casts_time(node):
        return node.this.copy()
    if isinstance(node, exp.Paren) and not isinstance(node.this, exp.Query):
        return node.this.copy()
    defaults = node.expressions if isinstance(node, exp.Coalesce) else []
    if defaults and all(isinstance(e, exp.Literal | exp.Null) for e in defaults):
        return node.this.copy()
    if isinstance(node, exp.Interval):
        unit = node.args.get("unit")
        value = node.this
        if unit is None and isinstance(value, exp.Literal) and value.is_string:
            match = re.match(r"^\s*[\d.]+\s*([a-zA-Z]+)\s*$", value.name)
            if match:
                return _interval(match.group(1))
        if unit is not None:
            return _interval(unit.name)
    if isinstance(node, exp.ToDays):
        return _interval("DAY")
    if isinstance(node, exp.Anonymous) and str(node.this).lower() in _TIME_FUNCTIONS:
        return _interval(_TIME_FUNCTIONS[str(node.this).lower()])
    return node


def _resolve_relations(tree: exp.Expression, relations: Mapping[str, str]) -> None:
    """Qualified names become the catalog table they stand for (CTE references are left alone)."""
    ctes = {c.alias_or_name for c in tree.find_all(exp.CTE)}
    for table in tree.find_all(exp.Table):
        if table.name and not (table.name in ctes and not table.db):
            table.set("this", exp.to_identifier(_table_target(table, relations)))
            table.set("db", None)
            table.set("catalog", None)


def _passthrough_table(select: exp.Expression) -> exp.Table | None:
    """The table a `SELECT * FROM t` (nothing else) passes through, else None."""
    if not isinstance(select, exp.Select) or len(select.expressions) != 1:
        return None
    if not isinstance(select.expressions[0], exp.Star) or select.expressions[0].args.get("except_"):
        return None
    extras = {k for k, v in select.args.items() if v and k not in ("expressions", "from_")}
    source = select.args.get("from_")
    if extras or source is None or not isinstance(source.this, exp.Table):
        return None
    return source.this


def _inline_passthrough(tree: exp.Expression) -> None:
    """Inline CTEs and derived tables that only pass one table through."""
    changed = True
    while changed:
        changed = False
        for cte in list(tree.find_all(exp.CTE)):
            table = _passthrough_table(cte.this)
            if table is None:
                continue
            name = cte.alias_or_name
            for ref in list(tree.find_all(exp.Table)):
                if (
                    ref.name == name
                    and not ref.db
                    and ref is not table
                    and ref.find_ancestor(exp.CTE) is not cte
                ):
                    inner = table.copy()
                    inner.set(
                        "alias",
                        ref.args.get("alias") or exp.TableAlias(this=exp.to_identifier(name)),
                    )
                    ref.replace(inner)
            with_ = cte.parent
            cte.pop()
            if with_ is not None and not with_.expressions:
                with_.pop()
            changed = True
        for sub in list(tree.find_all(exp.Subquery)):
            table = _passthrough_table(sub.this)
            if table is None or not isinstance(sub.parent, (exp.From, exp.Join)):
                continue
            inner = table.copy()
            alias = sub.args.get("alias")
            inner.set(
                "alias",
                alias.copy() if alias else exp.TableAlias(this=exp.to_identifier(table.name)),
            )
            sub.replace(inner)
            changed = True


def _is_star_select(select: exp.Expression) -> bool:
    """`SELECT * FROM <one source>` and nothing else."""
    if not isinstance(select, exp.Select) or len(select.expressions) != 1:
        return False
    extras = {k for k, v in select.args.items() if v and k not in ("expressions", "from_", "with_")}
    return isinstance(select.expressions[0], exp.Star) and not extras and select.args.get("from_")


def _unwrap(tree: exp.Expression) -> exp.Expression:
    """`SELECT * FROM (q) w` and `WITH q AS (...) SELECT * FROM q` are the query they wrap."""
    while _is_star_select(tree):
        source = tree.args["from_"].this
        with_ = tree.args.get("with_")
        if isinstance(source, exp.Subquery) and isinstance(source.this, exp.Select) and not with_:
            tree = source.this.copy()
        elif (
            isinstance(source, exp.Table)
            and with_ is not None
            and len(with_.expressions) == 1
            and with_.expressions[0].alias_or_name == source.name
            and isinstance(with_.expressions[0].this, exp.Select)
        ):
            tree = with_.expressions[0].this.copy()
        else:
            break
    return tree


def _expand_groups(tree: exp.Expression) -> None:
    """`GROUP BY ALL` and ordinals become the expressions they stand for."""
    for select in tree.find_all(exp.Select):
        group = select.args.get("group")
        if not group:
            continue
        projected = [e.this if isinstance(e, exp.Alias) else e for e in select.expressions]
        if group.args.get("all"):
            keep = [
                e.copy()
                for e in projected
                if not any(isinstance(n, (exp.AggFunc, exp.Window)) for n in e.walk())
            ]
            group.set("all", None)
            group.set("expressions", keep)
            continue
        items = []
        for item in group.expressions:
            if isinstance(item, exp.Literal) and not item.is_string and item.name.isdigit():
                index = int(item.name) - 1
                items.append(projected[index].copy() if 0 <= index < len(projected) else item)
            else:
                items.append(item)
        group.set("expressions", items)


def _plain(join: exp.Join) -> bool:
    return (
        not join.args.get("method")
        and not join.args.get("using")
        and join.args.get("on") is not None
        and isinstance(join.this, (exp.Table, exp.Subquery))
    )


def _simplify_joins(tree: exp.Expression) -> None:
    for select in tree.find_all(exp.Select):
        source, joins = select.args.get("from_"), select.args.get("joins") or []
        if source is not None and len(joins) == 1:
            join = joins[0]
            if (join.args.get("side") or "").upper() == "RIGHT" and _plain(join):
                left, right = source.this.copy(), join.this.copy()
                source.set("this", right)
                join.set("this", left)
                join.set("side", "LEFT")
        for join in joins:
            join.set("side", None)
            if str(join.args.get("kind") or "").upper() in ("INNER", "OUTER"):
                join.set("kind", None)


def _order_tables(tree: exp.Expression) -> None:
    """Put the tables of a plain join chain in a fixed order (by the table they read)."""
    for select in tree.find_all(exp.Select):
        source, joins = select.args.get("from_"), select.args.get("joins") or []
        if source is None or not joins or not all(_plain(j) for j in joins):
            continue
        items = [source.this, *[j.this for j in joins]]

        def key(item: exp.Expression) -> tuple[int, str]:
            return (0, item.name) if isinstance(item, exp.Table) else (1, "")

        ordered = [item.copy() for item in sorted(items, key=key)]
        source.set("this", ordered[0])
        for join, item in zip(joins, ordered[1:], strict=True):
            join.set("this", item)


class _Names:
    """Canonical names by order of first definition, one namespace per prefix."""

    def __init__(self) -> None:
        self.found: dict[tuple[str, str], str] = {}
        self.counts: Counter[str] = Counter()

    def define(self, prefix: str, name: str) -> str:
        if (prefix, name) not in self.found:
            self.counts[prefix] += 1
            self.found[(prefix, name)] = f"{prefix}{self.counts[prefix]}"
        return self.found[(prefix, name)]

    def get(self, prefix: str, name: str) -> str | None:
        return self.found.get((prefix, name))


def _number_tables(tree: exp.Expression, relations: Mapping[str, str]) -> None:
    ctes = {c.alias_or_name for c in tree.find_all(exp.CTE)}
    names = _Names()
    nodes = list(tree.walk(bfs=False))
    for node in nodes:
        if isinstance(node, exp.CTE):
            names.define("q", node.alias_or_name)
        elif isinstance(node, exp.Table) and node.name:
            if node.name in ctes and not node.db:
                if node.alias:
                    names.define("t", node.alias)
            else:
                names.define("t", node.alias or node.name)
        elif isinstance(node, exp.Subquery) and node.alias:
            names.define("t", node.alias)
    for node in nodes:
        if isinstance(node, exp.Table) and node.name:
            if node.name in ctes and not node.db:
                node.set("this", exp.to_identifier(names.get("q", node.name)))
                if node.alias:
                    node.set(
                        "alias", exp.TableAlias(this=exp.to_identifier(names.get("t", node.alias)))
                    )
            else:
                key = node.alias or node.name
                target = _table_target(node, relations)
                node.set("this", exp.to_identifier(target))
                node.set("db", None)
                node.set("catalog", None)
                node.set("alias", exp.TableAlias(this=exp.to_identifier(names.get("t", key))))
        elif isinstance(node, exp.Subquery) and node.alias:
            node.set("alias", exp.TableAlias(this=exp.to_identifier(names.get("t", node.alias))))
        elif isinstance(node, exp.CTE):
            node.set(
                "alias", exp.TableAlias(this=exp.to_identifier(names.get("q", node.alias_or_name)))
            )
        elif isinstance(node, exp.Column) and node.table:
            canonical = names.get("t", node.table) or names.get("q", node.table)
            if canonical:
                node.set("table", exp.to_identifier(canonical))


def _flatten(node: exp.Expression, kind: type) -> list[exp.Expression]:
    if isinstance(node, kind):
        return [*_flatten(node.this, kind), *_flatten(node.expression, kind)]
    return [node]


def _sort_expressions(tree: exp.Expression) -> None:
    """Fixed order for everything that is a set in SQL (children before parents)."""
    for node in reversed(list(tree.walk(bfs=False))):
        if isinstance(node, exp.GT):
            node.replace(exp.LT(this=node.expression.copy(), expression=node.this.copy()))
        elif isinstance(node, exp.GTE):
            node.replace(exp.LTE(this=node.expression.copy(), expression=node.this.copy()))
        elif isinstance(node, (exp.EQ, exp.NEQ, exp.Add, exp.Mul)):
            a, b = sorted([node.this, node.expression], key=lambda e: e.sql())
            node.set("this", a.copy())
            node.set("expression", b.copy())
        elif isinstance(node, (exp.And, exp.Or)):
            kind = type(node)
            parts = sorted(_flatten(node, kind), key=lambda e: e.sql())
            rebuilt = parts[0].copy()
            for part in parts[1:]:
                rebuilt = kind(this=rebuilt, expression=part.copy())
            if rebuilt.sql() != node.sql():
                node.replace(rebuilt)
        elif isinstance(node, exp.Group):
            node.set("expressions", sorted(node.expressions, key=lambda e: e.sql()))
        elif isinstance(node, exp.Select):
            body = lambda e: e.this if isinstance(e, exp.Alias) else e  # noqa: E731
            node.set("expressions", sorted(node.expressions, key=lambda e: body(e).sql()))
            joins = node.args.get("joins") or []
            if joins and all(_plain(j) for j in joins):
                ons = sorted((j.args["on"].copy() for j in joins), key=lambda e: e.sql())
                for join, on in zip(joins, ons, strict=True):
                    join.set("on", on)


def _number_features(tree: exp.Expression) -> None:
    names = _Names()
    nodes = list(tree.walk(bfs=False))
    for node in nodes:
        if isinstance(node, exp.Alias) and node.alias:
            names.define("c", node.alias)
    for node in nodes:
        if isinstance(node, exp.Alias) and node.alias:
            node.set("alias", exp.to_identifier(names.get("c", node.alias)))
        elif isinstance(node, exp.Column) and not node.table:
            canonical = names.get("c", node.name)
            if canonical:
                node.set("this", exp.to_identifier(canonical))
        elif isinstance(node, exp.Literal):
            node.set("this", "?")
            node.set("is_string", False)


def canonical_tree(
    sql: str, dialect: str = "duckdb", relations: Mapping[str, str] | None = None
) -> exp.Expression:
    """The query in the canonical form described in the module docstring."""
    relations = {k.lower(): v.lower() for k, v in (relations or {}).items()}
    tree = _parse(sql, dialect)
    _lower_identifiers(tree)
    _inline_passthrough(tree)
    tree = _unwrap(tree)
    for _ in range(2):  # a rewrite can expose another one (a CAST around a BETWEEN)
        tree = tree.transform(_rewrite)
    _resolve_relations(tree, relations)
    _expand_groups(tree)
    _simplify_joins(tree)
    _order_tables(tree)
    _number_tables(tree, relations)
    _sort_expressions(tree)
    _number_features(tree)
    return tree


def structure(sql: str, dialect: str = "duckdb", relations: Mapping[str, str] | None = None) -> str:
    """The canonical form of `sql` as text (what `ast_hash` hashes)."""
    return canonical_tree(sql, dialect, relations).sql(dialect=dialect)


def ast_hash(sql: str, dialect: str = "duckdb", relations: Mapping[str, str] | None = None) -> str:
    return hashlib.sha256(structure(sql, dialect, relations).encode()).hexdigest()


_PART_TYPES = (exp.Predicate, exp.Func, exp.Join, exp.Table, exp.CTE, exp.Subquery, exp.Window)


def parts(
    sql: str, dialect: str = "duckdb", relations: Mapping[str, str] | None = None
) -> frozenset:
    """The pieces of the canonical form that similarity compares."""
    tree = canonical_tree(sql, dialect, relations)
    found = {n.sql(dialect=dialect) for n in tree.walk(bfs=False) if isinstance(n, _PART_TYPES)}
    for select in tree.find_all(exp.Select):
        found.update(f"select {e.sql(dialect=dialect)}" for e in select.expressions)
    return frozenset(found)


def similarity(a: frozenset, b: frozenset) -> float:
    return len(a & b) / len(a | b) if a | b else 1.0


def load_directory(directory: Path, split: str = "candidate") -> list[Template]:
    """The templates in `directory` (`*.sql` in the template format), for an author's output that
    lives outside the repository."""
    if not directory.is_dir():
        raise ValueError(f"{directory} is not a directory")
    return [load_template(p, split) for p in sorted(directory.glob("*.sql"))]


@dataclass(frozen=True)
class _Form:
    """One distinct query of a template, in the forms the comparisons use."""

    case_id: str
    sql_text: str
    structure_hash: str
    parts: frozenset


def _forms(
    templates: Iterable[Template], dialect: str, relations: Mapping[str, str] | None
) -> dict[str, list[_Form]]:
    """Distinct forms per template id (renderings that canonicalise alike are one)."""
    forms: dict[str, list[_Form]] = {}
    for template in templates:
        seen: set[str] = set()
        for case in render(template):
            digest = ast_hash(case.sql, dialect, relations)
            if digest in seen:
                continue
            seen.add(digest)
            forms.setdefault(template.template_id, []).append(
                _Form(
                    case.case_id,
                    normalized_sql(case.sql, dialect),
                    digest,
                    parts(case.sql, dialect, relations),
                )
            )
    return forms


def find_duplicates(
    candidate: Iterable[Template],
    reference: Mapping[str, Iterable[Template]],
    *,
    dialect: str = "duckdb",
    relations: Mapping[str, str] | None = None,
    similar_at: float = SIMILAR_AT,
) -> list[Duplicate]:
    """Every candidate query that matches a reference query by id, SQL, structure or similarity.

    A `similar` finding is the best match of that candidate query only, and only when no exact
    match was found for it.
    """
    candidate = list(candidate)
    ref_forms: list[tuple[str, _Form]] = []
    template_ids: dict[str, str] = {}
    for split, templates in reference.items():
        templates = list(templates)
        for template in templates:
            template_ids.setdefault(template.template_id, split)
        for form_list in _forms(templates, dialect, relations).values():
            ref_forms.extend((split, f) for f in form_list)
    by_sql = {f.sql_text: (f.case_id, split) for split, f in reversed(ref_forms)}
    by_ast = {f.structure_hash: (f.case_id, split) for split, f in reversed(ref_forms)}
    found: list[Duplicate] = []
    for template in candidate:
        if template.template_id in template_ids:  # case ids are `<template id>-<n>`
            split = template_ids[template.template_id]
            found.append(Duplicate("id", template.template_id, template.template_id, split))
    for form_list in _forms(candidate, dialect, relations).values():
        for form in form_list:
            exact = False
            for kind, table, key in (
                ("sql", by_sql, form.sql_text),
                ("ast", by_ast, form.structure_hash),
            ):
                if key in table:
                    found.append(Duplicate(kind, form.case_id, *table[key]))
                    exact = True
            if exact:
                continue
            score, best = max(
                ((similarity(form.parts, f.parts), (f.case_id, split)) for split, f in ref_forms),
                default=(0.0, None),
            )
            if best is not None and score >= similar_at:
                found.append(Duplicate("similar", form.case_id, best[0], best[1], round(score, 3)))
    return found


def find_internal(
    candidate: Iterable[Template],
    *,
    dialect: str = "duckdb",
    relations: Mapping[str, str] | None = None,
    similar_at: float = SIMILAR_AT,
) -> list[tuple[str, str, str, float]]:
    """Pairs of candidate templates that are near-copies: (a, b, kind, score).

    The same canonical query is reported whatever the labels (one of them is wrong, or both are
    the same template). A merely similar pair is reported only when the labels are equal: a clean
    twin of a leaky shape is the point of a hard negative.
    """
    templates = sorted(candidate, key=lambda t: t.template_id)
    forms = _forms(templates, dialect, relations)
    label = {t.template_id: t.leak_type for t in templates}
    pairs = []
    for index, first in enumerate(templates):
        for second in templates[index + 1 :]:
            best, kind = 0.0, ""
            for a in forms.get(first.template_id, []):
                for b in forms.get(second.template_id, []):
                    if a.structure_hash == b.structure_hash:
                        best, kind = 1.0, "ast"
                        break
                    if label[first.template_id] == label[second.template_id]:
                        score = similarity(a.parts, b.parts)
                        if score > best and score >= similar_at:
                            best, kind = score, "similar"
                if kind == "ast":
                    break
            if kind:
                pairs.append((first.template_id, second.template_id, kind, round(best, 3)))
    return pairs


def distinct_templates(
    candidate: Iterable[Template],
    *,
    dialect: str = "duckdb",
    relations: Mapping[str, str] | None = None,
    similar_at: float = SIMILAR_AT,
) -> list[Template]:
    """One template per group of near-copies (the first by id): what the quotas count."""
    templates = sorted(candidate, key=lambda t: t.template_id)
    parent = {t.template_id: t.template_id for t in templates}

    def root(name: str) -> str:
        while parent[name] != name:
            parent[name] = parent[parent[name]]
            name = parent[name]
        return name

    label = {t.template_id: t.leak_type for t in templates}
    for a, b, _, _ in find_internal(
        templates, dialect=dialect, relations=relations, similar_at=similar_at
    ):
        if label[a] == label[b]:
            parent[root(b)] = root(a)
    kept = {root(t.template_id) for t in templates}
    return [t for t in templates if t.template_id in kept]


def one_per_template(duplicates: Iterable[Duplicate]) -> dict[str, list[Duplicate]]:
    """Duplicates grouped by candidate template (case ids are `<template>-<n>`)."""
    grouped: dict[str, list[Duplicate]] = {}
    for d in duplicates:
        grouped.setdefault(re.sub(r"-\d{3}$", "", d.candidate), []).append(d)
    return grouped


# --- shape quotas ---------------------------------------------------------------------------


def is_dbt_style(template: Template) -> bool:
    """A qualified relation name, an SCD2 table, or a chain of at least three CTEs."""
    case = render(template)[0]
    tree = _parse(case.sql, "duckdb")
    if len(list(tree.find_all(exp.CTE))) >= DBT_CTE_DEPTH:
        return True
    for table in tree.find_all(exp.Table):
        if table.name.lower() == "plan_history" or table.db:
            return True
    return False


def shape_counts(templates: list[Template]) -> dict[str, object]:
    """Templates per type, the dbt-style share, and what is short of the brief's quotas.

    Pass the distinct templates (`distinct_templates`) so near-copies are counted once."""
    per_type = Counter(t.leak_type for t in templates)
    dbt = sum(is_dbt_style(t) for t in templates)
    short = []
    if per_type["clean"] < MIN_CLEAN:
        short.append(f"clean templates: {per_type['clean']} < {MIN_CLEAN}")
    for leak in LEAK_TYPES[1:]:
        if per_type[leak] < MIN_LEAKY_PER_TYPE:
            short.append(f"{leak} templates: {per_type[leak]} < {MIN_LEAKY_PER_TYPE}")
    share = dbt / len(templates) if templates else 0.0
    if share < MIN_DBT_SHARE:
        short.append(f"dbt-style share: {share:.2f} < {MIN_DBT_SHARE:.2f}")
    return {
        "templates": len(templates),
        "per_type": {t: per_type[t] for t in LEAK_TYPES},
        "dbt_style": dbt,
        "dbt_share": round(share, 4),
        "short_of_quota": short,
    }


# --- command line ---------------------------------------------------------------------------


def _relations_of(split: str) -> dict[str, str]:
    spec = yaml.safe_load(load_splits()[split].spec.read_text(encoding="utf-8"))
    return {str(k): str(v) for k, v in (spec.get("relations") or {}).items()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m bench.leakage.duplicates", description=__doc__)
    where = parser.add_mutually_exclusive_group(required=True)
    where.add_argument("--candidate", help="a split of the repository, e.g. holdout_v2")
    where.add_argument("--candidate-dir", type=Path, help="a directory of template files")
    parser.add_argument(
        "--against",
        nargs="+",
        help="reference splits (default: every split that splits.yaml marks seen)",
    )
    parser.add_argument(
        "--relations-of",
        default="holdout_v2",
        help="the split whose spec's `relations` resolve qualified names (default: holdout_v2)",
    )
    parser.add_argument("--counts", action="store_true", help="also check the brief's quotas")
    args = parser.parse_args(argv)

    manifest = load_splits()
    reference_names = args.against or sorted(seen_splits())
    unknown = [n for n in reference_names if n not in manifest]
    if unknown:
        parser.error(f"unknown split(s) {unknown}; known: {sorted(manifest)}")
    if args.candidate:
        if args.candidate not in manifest:
            parser.error(f"unknown split {args.candidate!r}; known: {sorted(manifest)}")
        candidate = load_directory(TEMPLATES_DIR / args.candidate, args.candidate)
    else:
        candidate = load_directory(args.candidate_dir)
    if not candidate:
        print("no candidate templates found", flush=True)
        return 2
    reference = {n: load_directory(TEMPLATES_DIR / n, n) for n in reference_names}
    relations = _relations_of(args.relations_of)

    found = find_duplicates(candidate, reference, relations=relations)
    grouped = one_per_template(found)
    status = 0
    if grouped:
        status = 1
        print(f"{len(grouped)} candidate template(s) duplicate or resemble a reference template:")
        for template, hits in sorted(grouped.items()):
            best = max(hits, key=lambda d: (d.kind != "similar", d.score or 1.0))
            more = f" (+{len(hits) - 1} more)" if len(hits) > 1 else ""
            print(f"  {template}: {best}{more}")
    else:
        against = ", ".join(reference_names)
        print(f"no near-duplicates of {against} among {len(candidate)} templates")
    internal = find_internal(candidate, relations=relations)
    if internal:
        print(f"{len(internal)} pair(s) of candidate templates are near-copies of each other:")
        for a, b, kind, score in internal:
            print(f"  {a} ~ {b}: {kind} ({score:.2f})")
    distinct = distinct_templates(candidate, relations=relations)
    if len(distinct) != len(candidate):
        print(f"{len(candidate) - len(distinct)} template(s) collapsed into near-copies of another")
    if args.counts:
        counts = shape_counts(distinct)
        print(
            f"distinct templates: {counts['per_type']}; "
            f"dbt-style {counts['dbt_style']} ({counts['dbt_share']})"
        )
        for line in counts["short_of_quota"]:  # type: ignore[attr-defined]
            print(f"  short: {line}")
        status = status or int(bool(counts["short_of_quota"]))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
