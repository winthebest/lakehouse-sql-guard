"""The spec's `relations` mapping applies to static analysis too, not just to replay.

A SQL name such as `analytics.main.fct_events` whose bare table differs from the catalog table
(`events`) is read as that catalog table; names the mapping does not list behave as before.
"""

from sqlglot import exp

from sqlguard.core.parsing import parse_single
from sqlguard.leakage.analysis import analyze, resolve_relations
from sqlguard.leakage.fixes import Verification, suggest_fix
from sqlguard.leakage.spec import LeakageSpec
from sqlguard.leakage.validator import static_findings, validate

from .conftest import SCHEMA, SPEC

MAP = {"analytics.main.fct_events": "events", "analytics.main.fct_labels": "raw.labels"}
SEL = "SELECT l.user_id, l.label_ts, l.churned"
BARE = f"{SEL}, count(e.event_id) n FROM labels l LEFT JOIN events e ON e.user_id = l.user_id"
MAPPED = (
    f"{SEL}, count(e.event_id) n FROM analytics.main.fct_labels l "
    "LEFT JOIN analytics.main.fct_events e ON e.user_id = l.user_id"
)
GROUP = " GROUP BY 1, 2, 3"


def spec(**extra) -> LeakageSpec:
    return LeakageSpec.from_dict({**SPEC, **extra})


def rules(sql: str, **extra) -> list[str]:
    return sorted(f.rule_id for f in static_findings(sql, spec(**extra), SCHEMA))


def test_without_a_mapping_an_unknown_qualified_name_is_unclassified():
    assert "UNCLASSIFIED" in rules(MAPPED + GROUP)


def test_a_mapped_name_is_judged_like_the_catalog_table():
    assert "UNCLASSIFIED" not in rules(MAPPED + GROUP, relations=MAP)
    assert rules(MAPPED + GROUP, relations=MAP) == rules(BARE + GROUP)
    bounded = " AND e.event_ts < l.label_ts AND e.ingested_at < l.label_ts" + GROUP
    assert rules(MAPPED + bounded, relations=MAP) == rules(BARE + bounded) == []


def test_a_clean_query_through_mapped_names_is_clean():
    sql = (
        f"{SEL}, count(e.event_id) n FROM analytics.main.fct_labels l "
        "LEFT JOIN analytics.main.fct_events e ON e.user_id = l.user_id "
        "AND e.event_ts < l.label_ts AND e.ingested_at < l.label_ts" + GROUP
    )
    report = validate(sql, spec(relations=MAP), schema=SCHEMA)
    assert (report.verdict, report.findings) == ("clean", [])


def test_the_name_the_query_used_still_qualifies_its_columns():
    sql = (
        f"{SEL}, count(fct_events.event_id) n FROM analytics.main.fct_labels l "
        "LEFT JOIN analytics.main.fct_events ON fct_events.user_id = l.user_id" + GROUP
    )
    found = static_findings(sql, spec(relations=MAP), SCHEMA)
    assert {f.rule_id for f in found} <= {"L2", "L3"} and found
    assert not any(f.rule_id in ("PARSE", "UNCLASSIFIED") for f in found)


def test_quoted_and_capitalised_names_match_the_mapping():
    sql = MAPPED.replace("analytics.main.fct_events", '"Analytics"."Main"."FCT_Events"') + GROUP
    assert "UNCLASSIFIED" not in rules(sql, relations=MAP)


def test_names_the_mapping_does_not_list_are_left_alone():
    sql = (
        f"{SEL}, s.total_spend FROM labels l "
        "LEFT JOIN analytics.main.user_stats s ON s.user_id = l.user_id"
    )
    assert rules(sql, relations=MAP) == rules(sql)  # same as with no mapping at all
    analysis = analyze(sql, spec(relations=MAP), SCHEMA)
    assert {r.table for r in analysis.refs} == {"labels", "user_stats"}


def test_cte_names_are_never_renamed():
    sql = (
        "WITH fct_events AS (SELECT user_id, event_ts, ingested_at, event_id FROM events) "
        f"{SEL}, count(e.event_id) n FROM labels l LEFT JOIN fct_events e ON e.user_id = l.user_id"
        + GROUP
    )
    assert rules(sql, relations={"analytics.main.fct_events": "events"}) == rules(sql)


def test_resolve_relations_returns_the_same_tree_when_nothing_is_renamed():
    tree = parse_single(BARE)
    assert resolve_relations(tree, {}) is tree
    assert resolve_relations(tree, MAP) is tree
    mapped = parse_single(MAPPED)
    renamed = resolve_relations(mapped, MAP)
    assert renamed is not mapped
    assert [t.name for t in mapped.find_all(exp.Table)] == ["fct_labels", "fct_events"]
    assert [(t.db, t.name, t.alias) for t in renamed.find_all(exp.Table)] == [
        ("", "labels", "l"),
        ("", "events", "e"),
    ]


def test_node_meta_survives_the_rename_so_callers_can_map_findings_back():
    tree = parse_single(MAPPED)
    for node in tree.walk():
        node.meta["origin"] = "q"
    renamed = resolve_relations(tree, MAP)
    assert all(n.meta.get("origin") == "q" for n in renamed.find_all(exp.Table))


def test_fixes_for_mapped_names_are_verified_and_keep_the_queries_names():
    fixes = suggest_fix(MAPPED + GROUP, spec(relations=MAP), schema=SCHEMA)
    rewrites = [f for f in fixes if f.rewrites]
    assert rewrites and rewrites[-1].verification is Verification.STATIC_ONLY
    # the proof that only values change qualifies the query too: same answer as with bare names
    bare = suggest_fix(BARE + GROUP, spec(), schema=SCHEMA)
    assert [f.semantics for f in fixes] == [f.semantics for f in bare]
    after = rewrites[-1].after_sql
    assert "analytics.main.fct_events" in after and "e.event_ts < l.label_ts" in after
    assert "UNCLASSIFIED" not in {
        f.rule_id for f in static_findings(after, spec(relations=MAP), SCHEMA)
    }
