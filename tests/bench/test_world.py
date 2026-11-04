import hashlib
import json
from datetime import timedelta
from pathlib import Path

from bench.leakage.world import WorldConfig, build_world, write_timeline
from sqlguard.core.catalog import LakehouseCatalog


def _fingerprint(catalog: LakehouseCatalog) -> str:
    digest = hashlib.sha256()
    for name in catalog.table_names():
        rows = catalog.read(name).sort_by(
            [(c, "ascending") for c in catalog.read(name).column_names]
        )
        digest.update(json.dumps(rows.to_pylist(), default=str).encode())
    return digest.hexdigest()


def test_same_seed_same_world(tmp_path):
    small = WorldConfig(seed=7, n_users=40, n_days=20, label_start_day=8, label_end_day=12)
    a = LakehouseCatalog.local(tmp_path / "a")
    b = LakehouseCatalog.local(tmp_path / "b")
    build_world(a, small)
    build_world(b, small)
    assert _fingerprint(a) == _fingerprint(b)


def test_history_has_many_snapshots_with_logical_time(world):
    catalog, summary = world
    assert summary.snapshots["events"] > 1 and summary.snapshots["user_stats"] > 1
    times = [s.visible_at for s in catalog.snapshots("events")]
    assert times == sorted(times) and len(set(times)) == len(times)


def test_late_updates_present(world):
    _, summary = world
    assert summary.late_rate >= 0.05


def test_outcomes_only_visible_after_all_labels(world):
    catalog, _ = world
    latest_label = max(catalog.read("labels").column("label_ts").to_pylist())
    outcome_visible = catalog.snapshots("user_outcomes")[0].visible_at
    assert outcome_visible > latest_label


def test_timeline_file(world, tmp_path):
    catalog, _ = world
    path = tmp_path / "snapshot_timeline.json"
    write_timeline(catalog, path)
    timeline = json.loads(path.read_text())
    assert len(timeline["events"]) == len(catalog.snapshots("events"))


def test_spec_values_follow_from_the_world_parameters(world):
    """bench/leakage/spec.yaml declares what the generator guarantees, nothing read off a query."""
    import yaml

    from bench.leakage.world import declared_spec_fields

    catalog, _ = world
    declared = declared_spec_fields(WorldConfig())
    assert declared == {"max_ingest_delay": {"events": "8 days"}, "label_known_after": "14 days"}
    spec = yaml.safe_load(Path("bench/leakage/spec.yaml").read_text(encoding="utf-8"))
    assert {k: spec[k] for k in declared} == declared

    events = catalog.read("events").to_pylist()
    longest = max(r["ingested_at"] - r["event_ts"] for r in events)
    assert longest < timedelta(days=8)
    assert longest > timedelta(days=7)  # the bound is tight: late events do reach 7 nights
    outcomes = catalog.read("user_outcomes").to_pylist()
    assert max(r["days_to_churn"] for r in outcomes) <= 14


def test_late_events_use_the_configured_range(tmp_path):
    config = WorldConfig(seed=3, n_users=60, n_days=30, label_start_day=10, label_end_day=14)
    catalog = LakehouseCatalog.local(tmp_path)
    build_world(catalog, WorldConfig(**{**config.__dict__, "late_max_days": 2}))
    delays = [r["ingested_at"] - r["event_ts"] for r in catalog.read("events").to_pylist()]
    assert max(delays) < timedelta(days=3)
