"""Semantics-preserving rewrites of a query must not change what the static rules say about it.

Every query of the two dev splits, clean or leaky, is rewritten in ways that cannot change the rows
it reads: a CTE read once written as a derived table and back, a staging CTE or a qualified model
name in front of a table, the spine split into two UNION branches, an inner join's ON moved to
WHERE, the whole query wrapped in a SELECT *. The verdict on the rewrite must be the verdict on the
original (which of L2 and L3 names a missing bound may move with the scope of the aggregate). A
difference is a bug in how the analysis follows a bound or a window through that structure,
whichever way it points: a clean query turned leaky is a false positive that another way of writing
it exposes, a leaky one turned clean is a miss.
"""

from collections.abc import Callable

import pytest
from sqlglot import exp

from bench.leakage.cases import generate_cases
from sqlguard.core.parsing import parse_single
from sqlguard.leakage.spec import LeakageSpec
from sqlguard.leakage.validator import validate

from .conftest import SCHEMA

SPEC = LeakageSpec.from_dict(
    {
        "label_table": "labels",
        "entity_keys": ["user_id"],
        "prediction_ts": "label_ts",
        "label_columns": ["churned"],
        "time_columns": {"events": "event_ts", "user_stats": "updated_at"},
        "availability_columns": {"events": "ingested_at"},
        "max_ingest_delay": {"events": "8 days"},
        "label_known_after": "14 days",
        "static_tables": ["entities"],
        "outcome_columns": ["user_outcomes.refund_after_churn", "user_outcomes.days_to_churn"],
        "relations": {
            "analytics.marts.fct_training_spine": "labels",
            "analytics.staging.stg_events": "events",
            "analytics.staging.stg_user_stats": "user_stats",
            "analytics.staging.stg_entities": "entities",
            "analytics.staging.stg_user_outcomes": "user_outcomes",
        },
    }
)
QUALIFIED = {
    "labels": "analytics.marts.fct_training_spine",
    "events": "analytics.staging.stg_events",
    "user_stats": "analytics.staging.stg_user_stats",
    "entities": "analytics.staging.stg_entities",
    "user_outcomes": "analytics.staging.stg_user_outcomes",
}
Rewrite = Callable[[exp.Expression], exp.Expression | None]


def _with(tree: exp.Expression) -> exp.With | None:
    return tree.args.get("with_")


def _tables(tree: exp.Expression, name: str | None = None) -> list[exp.Table]:
    return [
        t
        for t in tree.find_all(exp.Table)
        if t.name and not t.db and (name is None or t.name == name)
    ]


def cte_to_derived(tree: exp.Expression) -> exp.Expression | None:
    """Inline every CTE that is read exactly once as a derived table."""
    tree = tree.copy()
    changed = False
    clause = _with(tree)
    if clause is None or clause.args.get("recursive"):
        return None
    for cte in list(clause.expressions):
        uses = [t for t in _tables(tree, cte.alias) if t.parent is not cte]
        if len(uses) != 1 or not isinstance(uses[0].parent, exp.From | exp.Join):
            continue
        use = uses[0]
        derived = exp.Subquery(
            this=cte.this.copy(),
            alias=exp.TableAlias(this=exp.to_identifier(use.alias or cte.alias)),
        )
        use.replace(derived)
        cte.pop()
        changed = True
    if not clause.expressions:
        tree.set("with_", None)
    return tree if changed else None


def derived_to_cte(tree: exp.Expression) -> exp.Expression | None:
    """Hoist the derived tables of the outermost SELECT and of every CTE body into CTEs (a CTE
    that already exists stays ahead of the ones that read it)."""
    if not isinstance(tree, exp.Select):
        return None
    tree = tree.copy()
    clause = _with(tree) or exp.With(expressions=[])
    ctes: list[exp.CTE] = clause.args["expressions"]
    count = 0

    def hoist(select: exp.Select, before: exp.CTE | None) -> None:
        nonlocal count
        for item in [select.args.get("from_"), *(select.args.get("joins") or [])]:
            sub = item.this if item is not None else None
            if not (
                isinstance(sub, exp.Subquery) and sub.alias and item.args.get("side") != "LATERAL"
            ):
                continue
            name = f"hoisted_{count}"
            count += 1
            cte = exp.CTE(this=sub.this.copy(), alias=exp.TableAlias(this=exp.to_identifier(name)))
            position = ctes.index(before) if before is not None else len(ctes)
            ctes.insert(position, cte)
            sub.replace(
                exp.Table(
                    this=exp.to_identifier(name),
                    alias=exp.TableAlias(this=exp.to_identifier(sub.alias)),
                )
            )

    for cte in list(ctes):
        if isinstance(cte.this, exp.Select):
            hoist(cte.this, cte)
    hoist(tree, None)
    if not count:
        return None
    tree.set("with_", clause)
    return tree


def wrap(tree: exp.Expression) -> exp.Expression | None:
    return exp.select("*").from_(
        exp.Subquery(this=tree.copy(), alias=exp.TableAlias(this=exp.to_identifier("wrapped")))
    )


def staging_cte(tree: exp.Expression) -> exp.Expression | None:
    """Read every temporal table through a pass-through CTE, as a staging model does."""
    if not isinstance(tree, exp.Select) or any(
        c.alias.startswith("stg_") for c in tree.find_all(exp.CTE)
    ):
        return None
    tree = tree.copy()
    names = {t.name for t in _tables(tree) if t.name in ("events", "user_stats")}
    if not names:
        return None
    for table in _tables(tree):
        if table.name in names:
            if not table.alias:
                table.set("alias", exp.TableAlias(this=exp.to_identifier(table.name)))
            table.set("this", exp.to_identifier(f"stg_{table.name}"))
    clause = _with(tree) or exp.With(expressions=[])
    staged = [
        exp.CTE(
            this=exp.select("*").from_(name),
            alias=exp.TableAlias(this=exp.to_identifier(f"stg_{name}")),
        )
        for name in sorted(names)
    ]
    clause.set("expressions", [*staged, *clause.expressions])
    tree.set("with_", clause)
    return tree


def qualified_names(tree: exp.Expression) -> exp.Expression | None:
    """Name every table by its qualified model name from the spec's `relations`."""
    tree = tree.copy()
    ctes = {c.alias for c in tree.find_all(exp.CTE)}
    changed = False
    for table in _tables(tree):
        if table.name in QUALIFIED and table.name not in ctes:
            if not table.alias:
                table.set("alias", exp.TableAlias(this=exp.to_identifier(table.name)))
            catalog, schema, name = QUALIFIED[table.name].split(".")
            table.set("this", exp.to_identifier(name))
            table.set("db", exp.to_identifier(schema))
            table.set("catalog", exp.to_identifier(catalog))
            changed = True
    return tree if changed else None


def split_spine(tree: exp.Expression) -> exp.Expression | None:
    """Read the spine as two UNION ALL branches that together hold every row."""
    if not isinstance(tree, exp.Select) or tree.args.get("from_") is None:
        return None
    source = tree.args["from_"].this
    if not isinstance(source, exp.Table) or source.name != "labels" or source.db:
        return None
    tree = tree.copy()
    source = tree.args["from_"].this
    alias = source.alias or "labels"
    halves = " UNION ALL ".join(
        f"SELECT * FROM labels WHERE user_id % 2 = {parity}" for parity in (0, 1)
    )
    subquery = parse_single(f"SELECT * FROM ({halves}) AS {alias}", "duckdb").args["from_"].this
    source.replace(subquery)
    return tree


def on_to_where(tree: exp.Expression) -> exp.Expression | None:
    """Move the ON of every inner join of the outermost SELECT into its WHERE."""
    if not isinstance(tree, exp.Select):
        return None
    tree = tree.copy()
    moved = []
    for join in tree.args.get("joins") or []:
        side = str(join.args.get("side") or "").upper()
        kind = str(join.args.get("kind") or "").upper()
        if join.args.get("on") is not None and not side and kind in ("", "INNER"):
            moved.append(join.args["on"].copy())
            join.set("on", exp.true())
    if not moved:
        return None
    for predicate in moved:
        tree = tree.where(predicate, copy=False)
    return tree


REWRITES: dict[str, Rewrite] = {
    "cte to derived table": cte_to_derived,
    "derived table to cte": derived_to_cte,
    "wrapped in select star": wrap,
    "staging cte in front of the tables": staging_cte,
    "qualified model names": qualified_names,
    "spine split in two union branches": split_spine,
    "inner join on moved to where": on_to_where,
}


def judged(sql: str | exp.Expression) -> tuple[str, set[str]]:
    """The static verdict and the rules behind it."""
    text = sql if isinstance(sql, str) else sql.sql(dialect="duckdb")
    report = validate(text, SPEC, schema=SCHEMA)
    return report.verdict, {f.rule_id for f in report.findings}


def seeds() -> list[tuple[str, str]]:
    """One rendering of every template of the two dev splits (the first), labelled by its type."""
    first: dict[str, tuple[str, str]] = {}
    for case in generate_cases(42, ("dev", "dev_v1b")):
        first.setdefault(case.template_id, (f"{case.template_id}:{case.leak_type}", case.sql))
    return sorted(first.values())


SEEDS = seeds()


def test_the_seed_set_has_clean_and_leaky_queries_of_every_type():
    types = {name.split(":")[1] for name, _ in SEEDS}
    assert {"clean", "L1", "L2", "L3", "L4", "L5", "L6", "L7"} <= types and len(SEEDS) >= 60


# few dev queries read a derived table in their outermost FROM
FEWER = {"derived table to cte": 3}


@pytest.mark.parametrize("name, rewrite", REWRITES.items(), ids=REWRITES.keys())
def test_a_semantics_preserving_rewrite_keeps_the_verdict(name, rewrite):
    disagreements = []
    applied = 0
    for seed, sql in SEEDS:
        base = judged(sql)
        if "PARSE" in base[1]:
            continue
        rewritten = rewrite(parse_single(sql, "duckdb"))
        if rewritten is None:
            continue
        applied += 1
        after = judged(rewritten)
        # the verdict is what must survive; which of L2 / L3 names a missing bound depends on
        # whether the aggregate sits in the scope of the table, so the rule ids may move
        if after[0] != base[0]:
            disagreements.append(
                f"{name}: {seed}: {base[0]} {sorted(base[1])} -> {after[0]} {sorted(after[1])}"
            )
    assert applied >= FEWER.get(name, 5), "the rewrite applied to too few queries to mean anything"
    assert not disagreements, "\n".join(disagreements)
