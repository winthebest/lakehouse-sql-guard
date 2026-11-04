"""The held-out spec reads dbt-style shapes: qualified relation names and the SCD2 table."""

import pytest

from bench.leakage.baseline import baseline_score, bind_relations
from bench.leakage.splits import load_splits
from sqlguard.leakage.replay import Replayer
from sqlguard.leakage.spec import LeakageSpec
from sqlguard.leakage.validator import validate

SPINE = "analytics.marts.fct_training_spine"
PLANS = "analytics.snapshots.plan_history_snapshot"

# name -> (sql, truth by construction)
QUERIES = {
    "plan_known_before_t": (
        f"""
        WITH plan_at AS (
          SELECT l.user_id, l.label_ts, p.plan, p.monthly_fee,
                 row_number() OVER (
                   PARTITION BY l.user_id, l.label_ts ORDER BY p.valid_from DESC
                 ) AS rn
          FROM {SPINE} AS l
          LEFT JOIN {PLANS} AS p
            ON p.user_id = l.user_id AND p.ingested_at < l.label_ts
           AND p.valid_from < l.label_ts
        )
        SELECT l.user_id, l.label_ts, l.churned, a.plan, a.monthly_fee
        FROM {SPINE} AS l
        JOIN plan_at AS a ON a.user_id = l.user_id AND a.label_ts = l.label_ts AND a.rn = 1
        """,
        False,
    ),
    "plan_with_load_margin": (
        """
        SELECT l.user_id, l.label_ts, l.churned, p.plan
        FROM marts.fct_training_spine AS l
        LEFT JOIN snapshots.plan_history_snapshot AS p
          ON p.user_id = l.user_id AND p.valid_from < l.label_ts - INTERVAL 8 DAY
         AND (p.valid_to IS NULL OR p.valid_to >= l.label_ts - INTERVAL 8 DAY)
        """,
        False,
    ),
    "events_qualified_ingested": (
        f"""
        SELECT l.user_id, l.label_ts, l.churned, COUNT(e.event_id) AS n
        FROM {SPINE} AS l
        LEFT JOIN analytics.staging.stg_events AS e
          ON e.user_id = l.user_id AND e.ingested_at < l.label_ts
        GROUP BY ALL
        """,
        False,
    ),
    "current_row_of_scd2": (
        f"""
        SELECT l.user_id, l.label_ts, l.churned, p.plan
        FROM {SPINE} AS l
        LEFT JOIN {PLANS} AS p ON p.user_id = l.user_id AND p.valid_to IS NULL
        """,
        True,
    ),
    "scd2_event_time_only": (
        f"""
        SELECT l.user_id, l.label_ts, l.churned, p.plan
        FROM {SPINE} AS l
        LEFT JOIN {PLANS} AS p
          ON p.user_id = l.user_id AND p.valid_from < l.label_ts
         AND (p.valid_to IS NULL OR p.valid_to >= l.label_ts)
        """,
        True,
    ),
    "events_qualified_unbounded": (
        f"""
        SELECT l.user_id, l.label_ts, l.churned, COUNT(e.event_id) AS n
        FROM staging.stg_events AS e JOIN {SPINE} AS l ON e.user_id = l.user_id
        GROUP BY ALL
        """,
        True,
    ),
}


@pytest.fixture(scope="module")
def spec():
    return LeakageSpec.from_yaml(load_splits()["holdout_v2"].spec)


@pytest.fixture(scope="module")
def replayer(world, spec):
    catalog, _ = world
    replayer = Replayer(catalog, spec, seed=42)
    bind_relations(replayer.current(), spec)
    return replayer


def test_the_spec_classifies_the_scd2_table_and_names_it_qualified(spec):
    assert spec.classify("plan_history") == "temporal"
    assert spec.time_columns["plan_history"] == "valid_from"
    assert spec.availability_columns["plan_history"] == "ingested_at"
    assert spec.relations["analytics.snapshots.plan_history_snapshot"] == "plan_history"
    assert spec.relations["marts.fct_training_spine"] == "labels"


@pytest.mark.parametrize("name", QUERIES)
def test_the_combined_verdict_follows_the_construction(name, world, spec, replayer):
    catalog, _ = world
    sql, leaky = QUERIES[name]
    report = validate(sql, spec, schema=catalog.schema_for(), replay=replayer)
    assert (report.verdict == "leaky") is leaky, (report.verdict, report.score, report.findings)
    if not leaky:
        assert report.verdict == "clean"


def test_the_baseline_can_run_qualified_queries(spec, replayer):
    """It scores 0 on a leaky one by design, but it must run rather than fail on the name."""
    sql, _ = QUERIES["current_row_of_scd2"]
    conn = replayer.current()
    assert len(conn.sql(sql).fetchall()) > 0
    assert baseline_score(conn, sql, spec) == 0.0


def test_a_spec_without_relations_cannot_resolve_the_qualified_names(world, spec):
    from bench.leakage.run import v1_spec_for

    catalog, _ = world
    sql, _ = QUERIES["events_qualified_unbounded"]
    v1_fields = v1_spec_for(load_splits()["holdout_v2"].spec)
    assert not v1_fields.relations
    report = validate(sql, v1_fields, schema=catalog.schema_for())
    assert report.verdict == "unverifiable"


def test_bind_relations_is_a_no_op_for_a_spec_without_relations(world):
    from bench.leakage.splits import load_splits as manifest

    catalog, _ = world
    plain = LeakageSpec.from_yaml(manifest()["dev"].spec)
    replayer = Replayer(catalog, plain, seed=42)
    bind_relations(replayer.current(), plain)
    names = replayer.current().sql("SELECT schema_name FROM duckdb_schemas()").fetchall()
    assert ("staging",) not in names
