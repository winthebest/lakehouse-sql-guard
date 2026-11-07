"""The event-log world (W2): one table that is spine, history and outcome holder, integer times,
a single series, nightly loads with late rows."""

import hashlib
import json
from datetime import timedelta
from pathlib import Path

import pytest
import yaml

from bench.leakage import audit
from bench.leakage import world as gen
from bench.leakage.cases import load_template
from sqlguard.core.catalog import LakehouseCatalog
from sqlguard.leakage.spec import LeakageSpec

SMALL = gen.EventLogConfig(seed=7, n_users=40, n_items=12, n_days=14)
DAY_MS = 86_400_000


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    catalog = LakehouseCatalog.local(tmp_path_factory.mktemp("w2"))
    summary = gen.build_event_log_world(catalog, SMALL)
    return catalog, summary


def _fingerprint(catalog: LakehouseCatalog) -> str:
    digest = hashlib.sha256()
    for name in catalog.table_names():
        table = catalog.read(name)
        rows = table.sort_by([(c, "ascending") for c in table.column_names])
        digest.update(json.dumps(rows.to_pylist(), default=str).encode())
    return digest.hexdigest()


def test_same_seed_same_world(tmp_path, built):
    catalog, _ = built
    other = LakehouseCatalog.local(tmp_path)
    gen.build_event_log_world(other, SMALL)
    assert _fingerprint(catalog) == _fingerprint(other)


def test_a_different_seed_is_a_different_world(tmp_path, built):
    catalog, _ = built
    other = LakehouseCatalog.local(tmp_path)
    gen.build_event_log_world(other, gen.EventLogConfig(**{**SMALL.__dict__, "seed": 8}))
    assert _fingerprint(catalog) != _fingerprint(other)


def test_the_tables_are_the_declared_ones_and_the_world_keeps_the_w1_tables_apart(built):
    catalog, summary = built
    assert set(catalog.table_names()) == set(gen.EVENT_LOG_SCHEMAS)
    assert not set(gen.EVENT_LOG_SCHEMAS) & {"events", "labels", "user_stats"}
    assert summary.n_interactions == catalog.read("interactions").num_rows > 100


def test_times_are_integers_in_the_declared_units(built):
    catalog, _ = built
    interactions = catalog.read("interactions")
    assert str(interactions.schema.field("ts_ms").type) == "int64"
    assert str(catalog.read("market_prices").schema.field("ts_s").type) == "int64"
    first = min(interactions.column("ts_ms").to_pylist())
    assert gen.from_epoch_ms(first) >= gen.START  # milliseconds, not seconds or microseconds
    prices = catalog.read("market_prices").column("ts_s").to_pylist()
    assert gen.START + timedelta(seconds=0) <= gen.EPOCH + timedelta(seconds=min(prices))


def test_the_prediction_row_and_the_history_are_the_same_table(built):
    catalog, _ = built
    rows = catalog.read("interactions").to_pylist()
    assert {r["correct"] for r in rows} == {0, 1}
    assert all(r["answer_shown_at"] > r["ts_ms"] for r in rows)  # the outcome comes after the row


def test_interactions_arrive_in_nightly_batches_with_late_rows(built):
    catalog, summary = built
    rows = catalog.read("interactions").to_pylist()
    assert all(r["loaded_ms"] % DAY_MS == 0 for r in rows)  # loaded at midnight
    assert all(r["loaded_ms"] >= r["ts_ms"] for r in rows)  # never before the row happened
    assert 0.03 <= summary.n_late / summary.n_interactions <= 0.25
    longest = max(r["loaded_ms"] - r["ts_ms"] for r in rows)
    assert longest < 3 * DAY_MS  # the bound the spec declares
    assert longest > 2 * DAY_MS  # and it is tight: late rows reach it
    assert summary.snapshots["interactions"] > 5
    times = [s.visible_at for s in catalog.snapshots("interactions")]
    assert times == sorted(times) and len(set(times)) == len(times)


def test_item_stats_are_loaded_with_a_delay_within_the_declared_bound(built):
    catalog, _ = built
    rows = catalog.read("item_stats").to_pylist()
    delays = [r["loaded_ms"] - r["updated_ms"] for r in rows]
    assert min(delays) == 0 and 0 < max(delays) < 3 * DAY_MS
    assert all(0 <= r["difficulty"] <= 1 for r in rows)


def test_market_prices_are_a_single_series_loaded_the_next_midnight(built):
    catalog, _ = built
    table = catalog.read("market_prices")
    assert table.column_names == ["ts_s", "price"]  # no entity key
    ts = sorted(table.column("ts_s").to_pylist())
    assert len(set(ts)) == len(ts)
    assert {b - a for a, b in zip(ts, ts[1:], strict=False)} == {900}
    first = [s for s in catalog.snapshots("market_prices")][0]
    assert first.visible_at == gen.START + timedelta(days=1)


def test_the_declared_bounds_follow_from_the_world_parameters():
    assert gen.event_log_ingest_bound(gen.EventLogConfig()) == "3 days"
    assert gen.event_log_ingest_bound(gen.EventLogConfig(late_max_days=5)) == "6 days"


def test_the_world_is_generated_once_per_directory(tmp_path):
    cfg_world = gen.event_log_world(tmp_path, 3)
    assert set(cfg_world.table_names()) == set(gen.EVENT_LOG_SCHEMAS)
    again = gen.event_log_world(tmp_path, 3)
    assert again.read("interactions").num_rows == cfg_world.read("interactions").num_rows


# --- the oracle: what was knowable at a prediction time ---------------------------------------

SPEC_PATH = Path("bench/leakage/spec_holdout_v4_w2.yaml")
PAIRS = Path("tests/bench/data/event_log_pairs")
RELATIONS = yaml.safe_load(SPEC_PATH.read_text(encoding="utf-8"))["relations"]


@pytest.fixture(scope="module")
def default_world(tmp_path_factory):
    return gen.event_log_world(tmp_path_factory.mktemp("w2-default"), 42)


@pytest.fixture(scope="module")
def oracle(default_world):
    return audit.EventLogAuditWorld.from_catalog(default_world)


def test_the_spec_matches_what_the_generator_guarantees(built):
    catalog, _ = built
    spec = yaml.safe_load(SPEC_PATH.read_text(encoding="utf-8"))
    cfg = gen.EventLogConfig()
    assert spec["max_ingest_delay"]["interactions"] == gen.event_log_ingest_bound(cfg)
    assert spec["max_ingest_delay"]["item_stats"] == gen.event_log_ingest_bound(cfg)
    assert spec["max_ingest_delay"]["market_prices"] == gen.event_log_price_bound(cfg)
    assert spec["label_known_after"] == 0
    assert set(spec["time_units"]) == set(spec["time_columns"])
    assert set(spec["time_columns"]) | set(spec["static_tables"]) == set(gen.EVENT_LOG_SCHEMAS)
    schema = {n: set(s.names) for n, s in gen.EVENT_LOG_SCHEMAS.items()}
    for table, column in spec["time_columns"].items():
        assert column in schema[table]
    for table, column in spec["availability_columns"].items():
        assert column in schema[table]
    assert spec["prediction_ts"] in schema["interactions"]
    for qualified, table in spec["relations"].items():
        assert table in schema, qualified
    LeakageSpec.from_yaml(SPEC_PATH)  # and the library accepts it


def test_known_at_t_holds_the_row_at_t_with_its_label_outcome_and_load_time_hidden(oracle):
    t = oracle.prediction_times()[len(oracle.prediction_times()) // 2]
    known = oracle.known_at(t)["interactions"].to_pylist()
    now = [r for r in known if r["ts_ms"] == t]
    assert now and all(
        r["correct"] is None and r["answer_shown_at"] is None and r["loaded_ms"] is None
        for r in now
    )
    earlier = [r for r in known if r["ts_ms"] < t]
    assert earlier and all(r["loaded_ms"] < t and r["correct"] is not None for r in earlier)
    assert not [r for r in known if r["ts_ms"] > t]


def test_rows_stamped_before_t_but_loaded_after_it_are_not_known(oracle):
    late = max(
        (r for r in oracle._rows if r["loaded_ms"] - r["ts_ms"] > 2 * DAY_MS),
        key=lambda r: r["loaded_ms"],
    )
    t = next(x for x in oracle.prediction_times() if x > late["ts_ms"])
    assert t < late["loaded_ms"], "pick a prediction time while the row is still in flight"
    ids = {r["interaction_id"] for r in oracle.known_at(t)["interactions"].to_pylist()}
    assert late["interaction_id"] not in ids
    final_ids = {r["interaction_id"] for r in oracle.final["interactions"].to_pylist()}
    assert late["interaction_id"] in final_ids


def test_an_answer_is_shown_only_after_it_was_given(oracle):
    shown = [r for r in oracle._rows if r["answer_shown_at"] - r["ts_ms"] > 60_000][5]
    t = next(x for x in oracle.prediction_times() if x > shown["ts_ms"])
    row = next(
        (
            r
            for r in oracle.known_at(t)["interactions"].to_pylist()
            if r["interaction_id"] == shown["interaction_id"]
        ),
        None,
    )
    if row is not None:  # the row itself was loaded by t
        assert (row["answer_shown_at"] is None) == (shown["answer_shown_at"] >= t)


def test_item_stats_and_prices_are_cut_at_their_load_times(oracle):
    t = oracle.prediction_times()[-1]
    known = oracle.known_at(t)
    assert max(known["item_stats"].column("loaded_ms").to_pylist()) < t
    midnight = (t // DAY_MS) * DAY_MS
    assert max(known["market_prices"].column("ts_s").to_pylist()) * 1000 < midnight
    assert known["items"].num_rows == oracle.final["items"].num_rows


def test_sampling_is_deterministic_and_k_zero_means_every_time(oracle):
    every = oracle.sample_points(0, 1)
    assert every == oracle.prediction_times()
    a, b = oracle.sample_points(30, 5), oracle.sample_points(30, 5)
    assert a == b and len(a) == 30 and a == sorted(a)
    assert set(a) <= set(every)
    assert oracle.sample_points(30, 6) != a


def test_a_split_cutoff_on_the_integer_spine_time_is_fixed_to_its_final_value(oracle):
    conn = audit._connection(oracle.final, RELATIONS)
    sql = (
        "SELECT s.user_id, s.ts_ms, s.correct FROM interactions s "
        "WHERE s.ts_ms < (SELECT MAX(ts_ms) FROM interactions)"
    )
    fixed, values = audit.fix_spine_cutoffs(sql, conn, RELATIONS, oracle.profile)
    assert values == [str(max(r["ts_ms"] for r in oracle._rows))]
    assert "SELECT MAX" not in fixed.upper()
    unchanged, none = audit.fix_spine_cutoffs(sql, conn, RELATIONS)  # the churn profile: no match
    assert unchanged == sql and none == []


def test_the_churn_world_audit_keeps_its_profile():
    assert audit.AuditWorld.profile is audit.W1_PROFILE
    assert audit.W1_PROFILE.passthrough == audit.PASSTHROUGH
    assert (audit.W1_PROFILE.spine_table, audit.W1_PROFILE.spine_time) == ("labels", "label_ts")


# --- one clean and one leaky pair per leak type, for checking the world (the label oracle) only

TYPES = ("L1", "L2", "L3", "L4", "L5", "L6", "L7")


def _pair(kind: str):
    clean = load_template(PAIRS / f"{kind.lower()}_clean.sql", "fixture")
    leaky = load_template(PAIRS / f"{kind.lower()}_leaky.sql", "fixture")
    return clean, leaky


def test_there_is_a_pair_for_each_leak_type():
    assert sorted(p.name for p in PAIRS.glob("*.sql")) == sorted(
        f"{t.lower()}_{k}.sql" for t in TYPES for k in ("clean", "leaky")
    )
    for kind in TYPES:
        clean, leaky = _pair(kind)
        assert (clean.leak_type, leaky.leak_type) == ("clean", kind)


@pytest.mark.parametrize("kind", TYPES[:5])
def test_the_oracle_agrees_with_the_label_of_each_pair_from_l1_to_l5(kind, oracle):
    points = oracle.sample_points(60, 42)
    clean, leaky = _pair(kind)
    results = audit.audit_templates([clean, leaky], oracle, RELATIONS, points)
    assert [(r.template_id, r.status) for r in results] == [
        (clean.template_id, "agree"),
        (leaky.template_id, "agree"),
    ]
    assert results[1].evidence["time"]  # the row where it shows
    assert results[0].renderings["leaky"] == 0 and results[1].renderings["clean"] == 0


@pytest.mark.parametrize("kind", ["L6", "L7"])
def test_l6_and_l7_are_left_to_the_authors(kind, oracle):
    clean, leaky = _pair(kind)
    results = audit.audit_templates([clean, leaky], oracle, RELATIONS, oracle.sample_points(20, 1))
    assert {r.status for r in results if r.label == kind} == {"not_auditable"}
    if kind == "L7":  # a clean template that reads the clock cannot be audited either
        assert {r.status for r in results} == {"not_auditable"}


def test_the_oracle_catches_a_leak_that_needs_the_late_rows(oracle):
    """An event-time bound that is the whole story: the final tables and the loaded-by-t tables
    differ only through rows that were stamped earlier and loaded later."""
    sql = (
        "SELECT s.user_id, s.ts_ms, s.correct, "
        "(SELECT COUNT(*) FROM interactions h "
        "WHERE h.user_id = s.user_id AND h.ts_ms < s.ts_ms) AS n "
        "FROM interactions s"
    )
    template = load_template(PAIRS / "l4_leaky.sql", "fixture")
    template = type(template)(template.template_id, "fixture", "L4", {}, sql)
    [result] = audit.audit_templates([template], oracle, RELATIONS, oracle.sample_points(60, 42))
    assert result.status == "agree" and result.evidence["rows_final"] > 0
