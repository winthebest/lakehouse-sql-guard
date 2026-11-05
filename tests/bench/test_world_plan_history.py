"""The SCD2 `plan_history` table: invariants, ground truth by construction, and the guarantee that
adding it left every other table of the world exactly as it was."""

import hashlib
import json
from datetime import datetime, timedelta

import pytest

from bench.leakage import world as gen
from bench.leakage.world import WorldConfig, build_world
from sqlguard.core.catalog import LakehouseCatalog

# Sorted-row hashes of the default world (seed 42), recorded before `plan_history` existed.
EXISTING_TABLES = {
    "entities": ("54232567a3ccdd46f9e07aa750a9a1aa31d694a123c06c2d83fad51134cb472a", 300),
    "events": ("9737a2a4f4d7b0b59006a7363e6c19296c8b6a60560a55f381a16c59df45c103", 12965),
    "labels": ("d51ff825fc3efc0e9a58b85e19d2b3dea555f5174dab192b66f59ed5ba070a43", 409),
    "user_outcomes": ("e38b75c30d90d85f065e3e79a001ac1ba22028089068def2c0cd044dfe05c80a", 68),
    "user_stats": ("8f303dd78cbd61cf2b82d4719c05ac434175b7877d21dca530ce29e182f07f27", 13133),
}
MICROSECOND = timedelta(microseconds=1)


def _digest(catalog: LakehouseCatalog, name: str) -> tuple[str, int]:
    data = catalog.read(name)
    rows = data.sort_by([(c, "ascending") for c in data.column_names])
    return hashlib.sha256(json.dumps(rows.to_pylist(), default=str).encode()).hexdigest(), len(rows)


def test_existing_tables_are_byte_identical(world):
    catalog, _ = world
    for name, expected in EXISTING_TABLES.items():
        assert _digest(catalog, name) == expected, name


def test_plan_history_is_generated_after_the_other_tables():
    assert list(gen.SCHEMAS)[-1] == "plan_history"


def test_same_seed_same_plan_history(tmp_path):
    small = WorldConfig(seed=7, n_users=40, n_days=20, label_start_day=8, label_end_day=12)
    a, b = LakehouseCatalog.local(tmp_path / "a"), LakehouseCatalog.local(tmp_path / "b")
    build_world(a, small)
    build_world(b, small)
    assert _digest(a, "plan_history") == _digest(b, "plan_history")


def test_every_user_has_one_open_version_and_closed_ones_chain(world):
    catalog, _ = world
    by_user: dict[int, list] = {}
    for v in world_versions(catalog):
        by_user.setdefault(v.user_id, []).append(v)
    assert set(by_user) == set(range(1, WorldConfig().n_users + 1))
    for versions in by_user.values():
        versions.sort(key=lambda v: v.valid_from)
        assert [v.valid_to is None for v in versions] == [False] * (len(versions) - 1) + [True]
        for earlier, later in zip(versions, versions[1:], strict=False):
            assert earlier.valid_to == later.valid_from  # validity intervals chain without gaps
            assert earlier.closed_at == later.ingested_at  # ...and close when the next one loads
            assert earlier.plan != later.plan
            assert earlier.ingested_at <= later.ingested_at


def world_versions(catalog):
    return gen.plan_versions(catalog)


def test_load_delay_stays_within_the_declared_bound(world):
    catalog, _ = world
    declared = gen.declared_plan_history_fields(WorldConfig())
    assert declared == {"max_ingest_delay": {"plan_history": "8 days"}}
    delays = [v.ingested_at - v.valid_from for v in world_versions(catalog)]
    assert max(delays) < timedelta(days=8)
    assert max(delays) > timedelta(days=7)  # the bound is tight: late loads do reach 7 nights
    assert all(d > timedelta(0) for d in delays)


def test_the_holdout_spec_repeats_what_the_generator_guarantees():
    from pathlib import Path

    import yaml

    spec = yaml.safe_load(Path("bench/leakage/spec_holdout_v2.yaml").read_text(encoding="utf-8"))
    declared = gen.declared_spec_fields()
    assert spec["label_known_after"] == declared["label_known_after"]
    assert spec["max_ingest_delay"] == {
        **declared["max_ingest_delay"],
        **gen.declared_plan_history_fields()["max_ingest_delay"],
    }
    assert spec["time_columns"]["plan_history"] == "valid_from"
    assert spec["availability_columns"]["plan_history"] == "ingested_at"


@pytest.mark.parametrize("night", [3, 15, 29, 44])
def test_snapshots_hold_exactly_what_the_oracle_says_was_loaded(world, night):
    """The commit history agrees with the generator's own ingestion times (the oracle never
    reads a snapshot; this checks the table was written the way the oracle assumes)."""
    catalog, _ = world
    versions = world_versions(catalog)
    for t in (gen.START + timedelta(days=night), gen.START + timedelta(days=night, hours=7)):
        snapshot_id = catalog.snapshot_as_of("plan_history", t - MICROSECOND)
        stored = sorted(
            catalog.read("plan_history", snapshot_id).to_pylist(),
            key=lambda r: (r["user_id"], r["valid_from"]),
        )
        now = t - MICROSECOND
        expected = sorted(
            (v.as_row(now) for v in versions if v.ingested_at <= now),
            key=lambda r: (r["user_id"], r["valid_from"]),
        )
        assert stored == expected


def test_a_closing_load_is_one_logical_time_for_both_its_snapshots(world):
    catalog, _ = world
    stamps = [s.visible_at for s in catalog.snapshots("plan_history")]
    assert stamps and stamps == sorted(stamps)
    assert len(set(stamps)) < len(stamps)  # overwrite = delete + append at the same logical time


def test_event_time_lookup_differs_from_the_known_plan_for_late_loads(world):
    """Ground truth for the L4 shape: a lookup bounded by `valid_from` alone sees versions that
    were not loaded yet, so it is leaky by construction; one bounded `max_ingest_delay` earlier
    only sees versions that were."""
    catalog, _ = world
    versions = world_versions(catalog)
    t = datetime(2026, 1, 20, 6)
    assert gen.late_plan_users(versions, t)
    for user, v in gen.effective_plans(versions, t - timedelta(days=8)).items():
        assert v.ingested_at < t, user


def test_a_final_valid_to_reveals_a_change_nobody_knew_of(world):
    """A version in effect at `t` that the finished table closes later: its `valid_to` is a future
    fact, though it sits in a row loaded before `t`."""
    catalog, _ = world
    versions = world_versions(catalog)
    t = datetime(2026, 1, 20, 6)
    exposed = gen.open_versions_at(versions, t)
    assert exposed
    assert all(v.ingested_at < t <= v.valid_to for v in exposed if v.ingested_at < t)
    assert any(v.closed_at is not None and v.closed_at >= t for v in exposed)


AS_OF_LOOKUP = """
SELECT l.user_id, l.label_ts, p.plan FROM labels l
LEFT JOIN plan_history p ON p.user_id = l.user_id AND p.ingested_at < l.label_ts
 AND p.valid_from < l.label_ts AND (p.valid_to IS NULL OR p.valid_to >= l.label_ts)
"""
NEWEST_LOADED = """
SELECT l.user_id, l.label_ts, p.plan FROM labels l
LEFT JOIN plan_history p ON p.user_id = l.user_id AND p.ingested_at < l.label_ts
QUALIFY row_number() OVER (PARTITION BY l.user_id, l.label_ts ORDER BY p.valid_from DESC) = 1
"""
LOAD_MARGIN = """
SELECT l.user_id, l.label_ts, p.plan FROM labels l
LEFT JOIN plan_history p ON p.user_id = l.user_id
 AND p.valid_from < l.label_ts - INTERVAL 8 DAY
 AND (p.valid_to IS NULL OR p.valid_to >= l.label_ts - INTERVAL 8 DAY)
"""


def _plans_that_differ_from_the_oracle(catalog, sql, margin=None):
    """Rows whose plan is not the one known at `t` (or, with a margin, not the plan in effect
    `margin` earlier by event time, which must itself have been loaded before `t`)."""
    from sqlguard.core.duck import connect_catalog

    versions = gen.plan_versions(catalog)
    wrong = []
    for user, t, plan in connect_catalog(catalog).sql(sql).fetchall():
        if margin is None:
            expected = gen.known_plans(versions, t).get(user)
        else:
            expected = gen.effective_plans(versions, t - margin).get(user)
            assert expected is None or expected.ingested_at < t  # loaded by the time it is read
        if (expected.plan if expected else None) != plan:
            wrong.append((user, t))
    return wrong


def test_the_as_of_lookup_on_valid_to_is_leaky_by_construction(world):
    """The usual point-in-time idiom, even with an ingestion bound, loses a plan the model could
    have read: the next version took effect before `t` but was loaded at or after it."""
    catalog, _ = world
    wrong = _plans_that_differ_from_the_oracle(catalog, AS_OF_LOOKUP)
    assert len(wrong) >= 5


def test_the_newest_loaded_version_is_the_known_plan_for_every_row(world):
    catalog, _ = world
    assert _plans_that_differ_from_the_oracle(catalog, NEWEST_LOADED) == []


def test_the_version_in_effect_a_load_margin_earlier_was_loaded(world):
    """A lookup at `t - 8 days` by event time returns the plan then, always loaded by `t`."""
    catalog, _ = world
    margin = timedelta(days=8)
    assert _plans_that_differ_from_the_oracle(catalog, LOAD_MARGIN, margin=margin) == []
