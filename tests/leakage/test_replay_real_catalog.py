"""Replay on catalogs that were not built for it: bulk-loaded tables, qualified relation names,
boundary and label-maturity settings, and time-split cutoffs."""

from dataclasses import replace
from datetime import datetime, timedelta

import pyarrow as pa
import pyarrow.compute as pc
import pytest

from bench.leakage.world import WorldConfig, build_world
from sqlguard.core.catalog import LOGICAL_TS_PROPERTY, LakehouseCatalog
from sqlguard.leakage.replay import Replayer, ReplayStatus
from sqlguard.leakage.spec import LeakageSpec
from sqlguard.leakage.validator import validate

BASE = LeakageSpec.from_dict(
    {
        "label_table": "labels",
        "entity_keys": ["user_id"],
        "prediction_ts": "label_ts",
        "label_columns": ["churned"],
        "time_columns": {"events": "event_ts"},
        "availability_columns": {"events": "ingested_at"},
        "static_tables": ["entities"],
    }
)
SEL = "SELECT l.user_id, l.label_ts, l.churned"
EVENT_SUM = (
    f"{SEL}, SUM(e.amount) AS amt FROM {{labels}} l LEFT JOIN {{events}} e "
    "ON e.user_id = l.user_id AND e.ingested_at < l.label_ts GROUP BY 1, 2, 3"
)
START = datetime(2026, 1, 1)


def label_rows(n_users: int = 12, days: int = 6) -> pa.Table:
    rows = [
        (u, START + timedelta(days=3 + d, hours=u % 5), u % 2)
        for u in range(1, n_users + 1)
        for d in range(days)
    ]
    return pa.table(
        {
            "user_id": pa.array([r[0] for r in rows], pa.int64()),
            "label_ts": pa.array([r[1] for r in rows], pa.timestamp("us")),
            "churned": pa.array([r[2] for r in rows], pa.int64()),
        }
    )


def event_rows(n_users: int = 12) -> pa.Table:
    rows = [
        (
            u * 100 + d,
            u,
            START + timedelta(days=d, hours=1),
            START + timedelta(days=d, hours=2),
            5.0,
        )
        for u in range(1, n_users + 1)
        for d in range(9)
    ]
    cols = list(zip(*rows, strict=True))
    return pa.table(
        {
            "event_id": pa.array(cols[0], pa.int64()),
            "user_id": pa.array(cols[1], pa.int64()),
            "event_ts": pa.array(cols[2], pa.timestamp("us")),
            "ingested_at": pa.array(cols[3], pa.timestamp("us")),
            "amount": pa.array(cols[4], pa.float64()),
        }
    )


def entity_rows(n_users: int = 12) -> pa.Table:
    return pa.table(
        {
            "user_id": pa.array(range(1, n_users + 1), pa.int64()),
            "country": pa.array(["VN"] * n_users),
        }
    )


@pytest.fixture
def single_load(tmp_path):
    """A SQL catalog whose tables were each loaded once, today (no logical-ts, one commit)."""
    catalog = LakehouseCatalog.local(tmp_path)
    catalog.create_table("labels", label_rows())
    catalog.create_table("events", event_rows())
    catalog.create_table("entities", entity_rows())
    return catalog


def committed_in_steps(tmp_path):
    """The same events, one commit per day, each stamped with the time it became readable."""
    catalog = LakehouseCatalog.local(tmp_path)
    catalog.create_table("labels", label_rows())
    catalog.create_table("entities", entity_rows())
    events = event_rows()
    catalog.create_empty("events", events.schema)
    for d in range(9):
        day = events.filter(
            pc.equal(
                pc.floor_temporal(events["ingested_at"], unit="day"),
                pa.scalar(START + timedelta(days=d), pa.timestamp("us")),
            )
        )
        catalog.table("events").append(
            day,
            snapshot_properties={
                LOGICAL_TS_PROPERTY: (START + timedelta(days=d, hours=2)).isoformat()
            },
        )
    return catalog


def sql(labels="labels", events="events") -> str:
    return EVENT_SUM.format(labels=labels, events=events)


def test_table_loaded_once_after_the_predictions_is_unverifiable_not_a_mismatch(single_load):
    result = Replayer(single_load, BASE).run(sql())
    assert result.status is ReplayStatus.UNVERIFIABLE
    assert "table history starts after prediction time" in result.reason
    assert "'events'" in result.reason and "commit time" in result.reason
    report = validate(sql(), BASE, catalog=single_load, replay=True)
    assert report.verdict == "unverifiable" and report.replay.status is ReplayStatus.UNVERIFIABLE
    assert not any(f.rule_id == "REPLAY" for f in report.findings)


def test_the_reason_names_the_visibility_field_that_was_used(tmp_path):
    catalog = LakehouseCatalog.local(tmp_path)
    catalog.create_table("labels", label_rows())
    catalog.create_empty("events", event_rows().schema)
    catalog.table("events").append(
        event_rows(), snapshot_properties={LOGICAL_TS_PROPERTY: "2026-01-20T00:00:00"}
    )
    result = Replayer(catalog, BASE).run(sql())
    assert result.status is ReplayStatus.UNVERIFIABLE
    assert "2026-01-20T00:00:00" in result.reason and "sqlguard.logical-ts" in result.reason


def test_a_table_with_history_covering_the_predictions_still_replays(tmp_path):
    catalog = committed_in_steps(tmp_path)
    result = Replayer(catalog, BASE, min_rows=1, min_points=1).run(sql())
    assert result.status is ReplayStatus.PASS


def test_late_table_blocks_only_the_queries_that_read_it(single_load):
    replayer = Replayer(single_load, BASE, min_rows=1, min_points=1)
    labels_only = f"{SEL}, u.country FROM labels l JOIN entities u ON u.user_id = l.user_id"
    assert replayer.run(labels_only).status is ReplayStatus.PASS
    assert replayer.run(sql()).status is ReplayStatus.UNVERIFIABLE


def test_static_tables_are_read_as_they_are_now(single_load):
    # `entities` was loaded today too, but is declared time-invariant
    spec = replace(BASE, static_tables=("entities",))
    result = Replayer(single_load, spec, min_rows=1, min_points=1).run(
        f"{SEL}, u.country FROM labels l JOIN entities u ON u.user_id = l.user_id"
    )
    assert result.status is ReplayStatus.PASS
    undeclared = replace(BASE, static_tables=(), time_columns={**BASE.time_columns})
    unknown = Replayer(single_load, undeclared, min_rows=1, min_points=1).run(
        f"{SEL}, u.country FROM labels l JOIN entities u ON u.user_id = l.user_id"
    )
    assert unknown.status is ReplayStatus.UNVERIFIABLE


@pytest.mark.parametrize(
    "labels, events",
    [
        ("db.labels", "db.events"),
        ('"db"."labels"', '"db"."events"'),
        ('"warehouse"."db"."labels"', '"warehouse"."db"."events"'),
        ('"warehouse"."db"."labels"', "events"),
    ],
)
def test_qualified_names_replay_like_bare_ones(tmp_path, labels, events):
    catalog = committed_in_steps(tmp_path)
    replayer = Replayer(catalog, BASE, min_rows=1, min_points=1)
    bare = replayer.run(sql())
    qualified = replayer.run(sql(labels, events))
    assert bare.status is ReplayStatus.PASS
    assert qualified.status is bare.status
    assert qualified.rows_compared == bare.rows_compared


def test_relation_map_names_a_dbt_style_three_part_name(tmp_path):
    catalog = committed_in_steps(tmp_path)
    query = sql('"analytics"."main"."labels"', '"analytics"."main"."events"')
    unmapped = Replayer(catalog, BASE, min_rows=1, min_points=1).run(query)
    assert unmapped.status is ReplayStatus.UNVERIFIABLE and "relations" in unmapped.reason
    spec = replace(
        BASE,
        relations={"analytics.main.labels": "labels", "analytics.main.events": "db.events"},
    )
    mapped = Replayer(catalog, spec, min_rows=1, min_points=1).run(query)
    assert mapped.status is ReplayStatus.PASS


def test_relation_map_to_a_missing_table_is_unverifiable(tmp_path):
    catalog = committed_in_steps(tmp_path)
    spec = replace(BASE, relations={"x.y.labels": "labels", "x.y.events": "nothing"})
    result = Replayer(catalog, spec).run(sql("x.y.labels", "x.y.events"))
    assert result.status is ReplayStatus.UNVERIFIABLE and "nothing" in result.reason


def test_qualified_leak_is_still_found(tmp_path):
    catalog = committed_in_steps(tmp_path)
    leaky = sql('"warehouse"."db"."labels"', '"warehouse"."db"."events"').replace(
        "e.ingested_at < l.label_ts", "e.event_ts < l.label_ts"
    )
    # an event becomes readable an hour after it happens, so an event-time bound reads rows
    # that were not there yet at prediction times inside that hour
    result = Replayer(catalog, BASE, min_rows=1, min_points=1).run(leaky)
    assert result.status is ReplayStatus.MISMATCH


# --- synthetic world: boundary, label maturity, cutoffs --------------------------------------


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    catalog = LakehouseCatalog.local(tmp_path_factory.mktemp("replay-real"))
    build_world(catalog, WorldConfig(seed=42))
    return catalog


@pytest.fixture(scope="module")
def spec():
    return LeakageSpec.from_yaml("bench/leakage/spec.yaml")


def test_inclusive_boundary_sees_facts_stamped_at_the_prediction_time(world, spec):
    query = sql().replace("e.ingested_at < l.label_ts", "e.ingested_at <= l.label_ts")
    strict = Replayer(world, replace(spec, boundary="strict"), seed=42).run(query)
    inclusive = Replayer(world, replace(spec, boundary="inclusive"), seed=42).run(query)
    assert strict.status is ReplayStatus.MISMATCH
    assert inclusive.status is ReplayStatus.PASS


MATURED = (
    f"{SEL}, (SELECT AVG(h.churned) FROM labels h WHERE h.user_id <> l.user_id "
    "AND h.label_ts < l.label_ts - INTERVAL {days} DAY) AS rate FROM labels l"
)


def test_labels_final_by_the_prediction_time_replay_with_their_values(world, spec):
    query = MATURED.format(days=14)
    without = replace(spec, label_known_after=None)
    assert Replayer(world, without, seed=42).run(query).status is ReplayStatus.MISMATCH
    known = replace(spec, label_known_after=14 * 86_400)
    assert Replayer(world, known, seed=42).run(query).status is ReplayStatus.PASS


def test_a_label_exactly_at_the_horizon_is_final_only_for_an_inclusive_spec(world, spec):
    query = MATURED.format(days=14).replace("h.label_ts <", "h.label_ts <=")
    known = replace(spec, label_known_after=14 * 86_400)
    assert Replayer(world, known, seed=42).run(query).status is ReplayStatus.MISMATCH
    inclusive = replace(known, boundary="inclusive")
    assert Replayer(world, inclusive, seed=42).run(query).status is ReplayStatus.PASS


def test_labels_not_yet_final_still_replay_as_unknown(world, spec):
    known = replace(spec, label_known_after=14 * 86_400)
    assert Replayer(world, known, seed=42).run(MATURED.format(days=3)).status is (
        ReplayStatus.MISMATCH
    )


CUT = (
    f"{SEL}, SUM(e.amount) AS amt FROM labels l LEFT JOIN events e "
    "ON e.user_id = l.user_id AND e.ingested_at < l.label_ts "
    "WHERE l.label_ts < {cut} GROUP BY 1, 2, 3"
)


def test_cutoff_from_the_label_times_replays_with_the_whole_label_table(world, spec):
    query = CUT.format(cut="(SELECT MAX(label_ts) FROM labels)")
    replayer = Replayer(world, spec, seed=42)
    assert replayer.run(query).status is ReplayStatus.PASS
    report = validate(query, spec, catalog=world, replay=replayer)
    assert report.verdict == "clean"


def test_cutoff_that_depends_on_the_row_is_not_kept_whole(world, spec):
    query = CUT.format(cut="(SELECT MAX(x.label_ts) FROM labels x WHERE x.user_id = l.user_id)")
    assert Replayer(world, spec, seed=42).run(query).status is ReplayStatus.MISMATCH


# --- row selection by later data: static and replay agree it leaks ------------------------------

WINDOW_14D = "AND e.event_ts >= l.label_ts - INTERVAL 14 DAY"


@pytest.mark.parametrize(
    "query",
    [
        # groups with any row stamped after the prediction are dropped from the full run only
        f"{SEL}, SUM(e.amount) AS amt FROM labels l LEFT JOIN events e ON e.user_id = l.user_id "
        f"{WINDOW_14D} GROUP BY l.user_id, l.label_ts, l.churned "
        "HAVING MAX(e.ingested_at) < l.label_ts",
        # only the entity's latest prediction survives the full run; a rerun at an earlier
        # prediction time sees that earlier row as the latest
        f"{SEL}, SUM(e.amount) AS amt FROM labels l LEFT JOIN events e ON e.user_id = l.user_id "
        f"AND e.ingested_at < l.label_ts {WINDOW_14D} GROUP BY l.user_id, l.label_ts, l.churned "
        "QUALIFY ROW_NUMBER() OVER (PARTITION BY l.user_id ORDER BY l.label_ts DESC) = 1",
    ],
    ids=["having max", "latest prediction per entity"],
)
def test_row_selection_by_later_data_is_a_leak_statically_and_in_replay(world, spec, query):
    replayer = Replayer(world, spec, seed=42)
    assert validate(query, spec, catalog=world).verdict == "leaky"
    assert replayer.run(query).status is ReplayStatus.MISMATCH
    assert validate(query, spec, catalog=world, replay=replayer).verdict == "leaky"
