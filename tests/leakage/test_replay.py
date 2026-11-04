from datetime import UTC, datetime

import pytest

from bench.leakage.cases import generate_cases
from bench.leakage.world import WorldConfig, build_world
from sqlguard.core.catalog import LakehouseCatalog
from sqlguard.leakage.replay import Replayer, ReplayStatus
from sqlguard.leakage.spec import LeakageSpec
from sqlguard.leakage.validator import validate

SPEC = LeakageSpec.from_yaml("bench/leakage/spec.yaml")
CLEAN_EVENTS = """
SELECT l.user_id, l.label_ts, l.churned, SUM(e.amount) AS amt
FROM labels l LEFT JOIN events e ON e.user_id = l.user_id
  AND e.ingested_at < l.label_ts AND e.event_ts >= l.label_ts - INTERVAL 14 DAY
GROUP BY l.user_id, l.label_ts, l.churned
"""
EVENT_TIME_ONLY = CLEAN_EVENTS.replace("e.ingested_at < l.label_ts", "e.event_ts < l.label_ts")


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    catalog = LakehouseCatalog.local(tmp_path_factory.mktemp("replay"))
    build_world(catalog, WorldConfig(seed=42))
    return catalog


@pytest.fixture(scope="module")
def replayer(world):
    return Replayer(world, SPEC)


def test_clean_query_replays_identically(replayer):
    result = replayer.run(CLEAN_EVENTS)
    assert result.status is ReplayStatus.PASS and result.rows_compared > 0


def test_late_data_detected_by_replay(replayer):
    result = replayer.run(EVENT_TIME_ONLY)
    assert result.status is ReplayStatus.MISMATCH
    assert set(result.mismatched_columns) == {"amt"}


def test_validator_combines_static_hint_and_replay(world, replayer):
    report = validate(EVENT_TIME_ONLY, SPEC, catalog=world, replay=replayer)
    assert report.verdict == "leaky"
    assert {"L4", "REPLAY"} <= {f.rule_id for f in report.findings}


def test_ground_truth_of_dev_cases(replayer):
    """Clean templates must replay identically; leaky ones must (almost always) diverge."""
    cases = generate_cases()
    for case in cases:
        if not case.is_leaky:
            result = replayer.run(case.sql)
            if replayer.current().sql(case.sql).fetchall():
                assert result.status is ReplayStatus.PASS, case.case_id
            else:  # a query that keeps no rows has nothing to compare: never a PASS
                assert result.status is ReplayStatus.UNVERIFIABLE, case.case_id
    leaky = [c for c in cases if c.is_leaky]
    manifested = sum(replayer.run(c.sql).status is ReplayStatus.MISMATCH for c in leaky)
    assert manifested / len(leaky) >= 0.95


def test_output_without_keys_is_unverifiable(replayer):
    result = replayer.run("SELECT l.churned FROM labels l")
    assert result.status is ReplayStatus.UNVERIFIABLE


def test_expired_history_is_unverifiable_not_clean(tmp_path):
    catalog = LakehouseCatalog.local(tmp_path)
    build_world(catalog, WorldConfig(seed=1, n_users=30, n_days=30))
    events = catalog.table("events")
    cutoff = catalog.snapshots("events")[-3].timestamp_ms
    events.maintenance.expire_snapshots().older_than(
        datetime.fromtimestamp(cutoff / 1000, tz=UTC)
    ).commit()
    assert catalog.history_truncated("events")

    result = Replayer(catalog, SPEC).run(CLEAN_EVENTS)
    assert result.status is ReplayStatus.UNVERIFIABLE
    assert "expired" in result.reason
    report = validate(CLEAN_EVENTS, SPEC, catalog=catalog, replay=True)
    assert report.verdict == "unverifiable"
