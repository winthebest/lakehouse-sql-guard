"""Near-duplicate and shape checks for a held-out template set.

A held-out split is only held out if its templates are not copies of what the rules were
developed against. Every case of the candidate templates is compared with every case of the
reference splits (by default the splits `splits.yaml` marks as seen) in three ways:

- `id`: the same template id;
- `sql`: the same SQL after parsing (comments, whitespace, keyword case and quoting do not count);
- `ast`: the same structure after aliases are renamed by order of first definition, qualified
  relation names are reduced to the catalog table they stand for, and literals are masked. Renaming
  a CTE or a feature, or changing 7 days to 14, does not hide a copy.

It is a heuristic with two blind spots, both on the side of missing a copy: it does not reorder
joins or predicates, and an alias reused for different tables in different scopes is numbered
once. Anything it flags is reported with the pair, so a reviewer decides.

`shape_counts` checks a candidate set against the author brief's quotas (clean and leaky templates
per type, share of dbt-style shapes).

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

from bench.leakage.cases import LEAK_TYPES, TEMPLATES_DIR, LeakCase, Template, load_template, render
from bench.leakage.splits import load_splits, seen_splits

MIN_CLEAN = 25
MIN_LEAKY_PER_TYPE = 8
MIN_DBT_SHARE = 0.30
DBT_CTE_DEPTH = 3  # a query with at least this many CTEs is dbt-style


@dataclass(frozen=True)
class Duplicate:
    kind: str  # "id" | "sql" | "ast"
    candidate: str  # case id
    reference: str  # case id
    reference_split: str

    def __str__(self) -> str:
        return (
            f"{self.kind}: {self.candidate} is a near-duplicate of "
            f"{self.reference_split}/{self.reference}"
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


def structure(sql: str, dialect: str = "duckdb", relations: Mapping[str, str] | None = None) -> str:
    """The alias-normalized, literal-masked form of `sql` that `ast_hash` hashes."""
    relations = {k.lower(): v.lower() for k, v in (relations or {}).items()}
    tree = _parse(sql, dialect)
    _lower_identifiers(tree)
    ctes = {c.alias_or_name for c in tree.find_all(exp.CTE)}

    names: dict[str, str] = {}

    def canon(name: str, prefix: str) -> str:
        return names.setdefault(f"{prefix}:{name}", f"{prefix}{len(names) + 1}")

    nodes = list(tree.walk())
    # definitions first, in tree order, so the numbering depends on structure only
    for node in nodes:
        if isinstance(node, exp.CTE):
            canon(node.alias_or_name, "q")
        elif isinstance(node, exp.Table) and node.name:
            if node.alias:
                canon(node.alias, "t")
            elif node.name not in ctes:
                canon(node.name, "t")  # an unaliased table is addressed by its own name
        elif isinstance(node, exp.Subquery) and node.alias:
            canon(node.alias, "t")
        elif isinstance(node, exp.Alias) and node.alias:
            canon(node.alias, "c")

    for node in nodes:
        if isinstance(node, exp.Table) and node.name:
            if node.name in ctes and not node.db:
                node.set("this", exp.to_identifier(names[f"q:{node.name}"]))
            else:
                node.set("this", exp.to_identifier(_table_target(node, relations)))
                node.set("db", None)
                node.set("catalog", None)
        if isinstance(node, (exp.Table, exp.Subquery)) and node.alias:
            node.set("alias", exp.TableAlias(this=exp.to_identifier(names[f"t:{node.alias}"])))
        elif isinstance(node, exp.CTE):
            cte_name = names[f"q:{node.alias_or_name}"]
            node.set("alias", exp.TableAlias(this=exp.to_identifier(cte_name)))
        elif isinstance(node, exp.Alias) and node.alias:
            node.set("alias", exp.to_identifier(names[f"c:{node.alias}"]))
        elif isinstance(node, exp.Column):
            qualifier = node.table
            if qualifier and f"t:{qualifier}" in names:
                node.set("table", exp.to_identifier(names[f"t:{qualifier}"]))
            elif qualifier and f"q:{qualifier}" in names:
                node.set("table", exp.to_identifier(names[f"q:{qualifier}"]))
            elif not qualifier and f"c:{node.name}" in names:
                node.set("this", exp.to_identifier(names[f"c:{node.name}"]))
        elif isinstance(node, exp.Literal):
            node.set("this", "?")
            node.set("is_string", False)
    return tree.sql(dialect=dialect)


def ast_hash(sql: str, dialect: str = "duckdb", relations: Mapping[str, str] | None = None) -> str:
    return hashlib.sha256(structure(sql, dialect, relations).encode()).hexdigest()


def _cases(templates: Iterable[Template]) -> list[LeakCase]:
    return [case for template in templates for case in render(template)]


def load_directory(directory: Path, split: str = "candidate") -> list[Template]:
    """The templates in `directory` (`*.sql` in the template format), for an author's output that
    lives outside the repository."""
    if not directory.is_dir():
        raise ValueError(f"{directory} is not a directory")
    return [load_template(p, split) for p in sorted(directory.glob("*.sql"))]


def find_duplicates(
    candidate: Iterable[Template],
    reference: Mapping[str, Iterable[Template]],
    *,
    dialect: str = "duckdb",
    relations: Mapping[str, str] | None = None,
) -> list[Duplicate]:
    """Every candidate case that matches a reference case by id, SQL or structure."""
    by_sql: dict[str, tuple[str, str]] = {}
    by_ast: dict[str, tuple[str, str]] = {}
    template_ids: dict[str, str] = {}
    for split, templates in reference.items():
        for template in templates:
            template_ids.setdefault(template.template_id, split)
            for case in render(template):
                by_sql.setdefault(normalized_sql(case.sql, dialect), (case.case_id, split))
                by_ast.setdefault(ast_hash(case.sql, dialect, relations), (case.case_id, split))
    found: list[Duplicate] = []
    for template in candidate:
        if template.template_id in template_ids:  # case ids are `<template id>-<n>`
            split = template_ids[template.template_id]
            found.append(Duplicate("id", template.template_id, template.template_id, split))
        for case in render(template):
            hits = (
                ("sql", by_sql.get(normalized_sql(case.sql, dialect))),
                ("ast", by_ast.get(ast_hash(case.sql, dialect, relations))),
            )
            for kind, hit in hits:
                if hit is not None:
                    found.append(Duplicate(kind, case.case_id, hit[0], hit[1]))
    return found


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
    """Templates per type, the dbt-style share, and what is short of the brief's quotas."""
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
        print(f"{len(grouped)} candidate template(s) duplicate a reference template:")
        for template, hits in sorted(grouped.items()):
            more = f" (+{len(hits) - 1} more)" if len(hits) > 1 else ""
            print(f"  {template}: {hits[0]}{more}")
    else:
        against = ", ".join(reference_names)
        print(f"no near-duplicates of {against} among {len(candidate)} templates")
    if args.counts:
        counts = shape_counts(candidate)
        print(
            f"templates: {counts['per_type']}; "
            f"dbt-style {counts['dbt_style']} ({counts['dbt_share']})"
        )
        for line in counts["short_of_quota"]:  # type: ignore[attr-defined]
            print(f"  short: {line}")
        status = status or int(bool(counts["short_of_quota"]))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
