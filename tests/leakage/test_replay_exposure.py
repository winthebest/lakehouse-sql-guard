"""Replay records, per table, how many later rows it hid from the replayed query, and a static
finding is settled only by a PASS that had such rows to be wrong about."""

import pytest

from bench.leakage.world import WorldConfig, build_world
from sqlguard.core.catalog import LakehouseCatalog
from sqlguard.leakage.replay import MIN_POINTS, MIN_ROWS, Replayer, ReplayStatus
from sqlguard.leakage.spec import LeakageSpec
from sqlguard.leakage.validator import validate

SPEC = LeakageSpec.from_yaml("bench/leakage/spec.yaml")
SEL = "SELECT l.user_id, l.label_ts, l.churned"
BOUNDED = (
    f"{SEL}, SUM(e.amount) AS a FROM labels l LEFT JOIN events e ON e.user_id = l.user_id "
    "AND e.ingested_at < l.label_ts GROUP BY 1, 2, 3"
)
# bounded, but written where the static analysis does not read it (inside an OR)
HIDDEN_BOUND = BOUNDED.replace(
    "AND e.ingested_at < l.label_ts", "AND (e.ingested_at < l.label_ts OR 1 = 0)"
)
STATIC_ONLY = f"{SEL}, ent.country FROM labels l JOIN entities ent ON ent.user_id = l.user_id"


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    catalog = LakehouseCatalog.local(tmp_path_factory.mktemp("exposure-world"))
    build_world(catalog, WorldConfig(seed=42))
    return catalog, Replayer(catalog, SPEC, seed=42)


def test_replay_counts_the_later_rows_of_each_table_it_hid(world):
    _, replayer = world
    result = replayer.run(BOUNDED)
    assert result.status is ReplayStatus.PASS
    assert result.exposure["events"] >= MIN_ROWS and result.exposed_points["events"] >= MIN_POINTS
    assert result.exposure["labels"] > 0  # the later prediction rows are hidden too


def test_a_table_with_no_history_has_no_exposure(world):
    _, replayer = world
    result = replayer.run(STATIC_ONLY)
    assert result.status is ReplayStatus.PASS
    assert result.exposure["entities"] == 0 and result.exposed_points["entities"] == 0


def test_a_leak_shows_up_in_the_rows_that_were_hidden(world):
    _, replayer = world
    leaky = BOUNDED.replace("AND e.ingested_at < l.label_ts", "")
    result = replayer.run(leaky)
    assert result.status is ReplayStatus.MISMATCH and result.exposure["events"] > 0


def test_a_pass_over_a_statically_flagged_query_is_reported_but_does_not_clear_it(world):
    catalog, replayer = world
    report = validate(HIDDEN_BOUND, SPEC, catalog=catalog, replay=replayer)
    assert {f.rule_id for f in report.findings} & {"L2", "L3"}
    assert report.replay.status is ReplayStatus.PASS and report.verdict == "leaky"
    assert any(f.evidence.get("replay_informative") for f in report.findings)


def test_label_table_exposure_counts_only_hidden_rows(world):
    """The pinned spine keeps the rows at the prediction time itself: at the latest time nothing
    later exists for those entities, so nothing is hidden."""
    import pyarrow as pa
    import pyarrow.compute as pc

    _, replayer = world
    ts = max(replayer._label_stamps.values())
    replayer.pinned(ts, history=True)
    column = replayer.labels.column("label_ts")
    at = replayer.labels.filter(pc.equal(column, pa.scalar(ts, type=column.type)))
    entities = {(u,) for u in at.column("user_id").to_pylist()}
    assert replayer._exposed("labels", ts, entities) == 0


def test_exposure_filters_on_every_entity_key_not_the_whole_table(world):
    from dataclasses import replace

    catalog, _ = world
    composite = Replayer(catalog, replace(SPEC, entity_keys=("user_id", "event_id")), seed=42)
    ts = max(composite._label_stamps.values())
    composite.pinned(ts, history=True)
    whole = composite._exposed("events", ts, None)
    one_user = composite._exposed("events", ts, {(3, -1)})
    assert whole > 0 and one_user == 0  # a key tuple that matches no event row hides nothing


# --- sampling the prediction times that have rows in flight -------------------------------------


def test_the_prediction_times_with_the_heaviest_tail_are_added_to_the_sample():
    from collections import Counter
    from datetime import datetime, timedelta

    from sqlguard.leakage.replay import sample_points

    base = datetime(2026, 1, 1)
    stamps = Counter({base + timedelta(hours=7 * i + 3): 1 for i in range(200)})
    plain = sample_points(stamps, 24, seed=5)
    heavy = [base + timedelta(hours=7 * i + 3) for i in (11, 97, 150)]
    weights = {ts: 1.0 + k for k, ts in enumerate(heavy)} | {base + timedelta(hours=3): 0.0}
    points = sample_points(stamps, 24, seed=5, in_flight=weights)
    assert set(plain) <= set(points) and set(heavy) <= set(points)  # nothing is taken away
    assert len(points) == len(set(points)) <= 24 + 3
    assert sample_points(stamps, 24, seed=5, in_flight=weights) == points  # deterministic
    # at most a third of the budget is added, heaviest first; a time of zero weight is never added
    many = {ts: 1.0 + i for i, ts in enumerate(stamps)}
    added = set(sample_points(stamps, 24, seed=5, in_flight=many)) - set(plain)
    assert len(added) == 8
    ranked = [ts for ts in sorted(many, key=many.get, reverse=True) if ts not in plain]
    assert added == set(ranked[:8])
    assert base + timedelta(hours=3) not in set(points) - set(plain)


def test_a_leak_one_day_wide_in_the_long_delay_tail_is_found(world):
    """The warehouse declares an 8-day worst case for loading; a 7-day event-time margin leaves a
    one-day window, and one prediction row of the world has an event in it. Uniform sampling of
    24 of 300 times misses it; sampling the times with the heaviest tail finds it."""
    _, replayer = world
    sql = (
        f"{SEL}, (SELECT COALESCE(SUM(e.amount), 0) FROM events e WHERE e.user_id = l.user_id "
        "AND e.event_ts < l.label_ts - INTERVAL 7 DAY "
        "AND e.event_ts >= l.label_ts - INTERVAL 30 DAY) AS spend FROM labels l"
    )
    result = replayer.run(sql)
    assert result.status is ReplayStatus.MISMATCH and result.rows_mismatched >= 1


# --- the cost of scoring the rows in flight ------------------------------------------------


def _synthetic_flight(n_rows: int, n_times: int):
    import random
    from datetime import datetime, timedelta

    import pyarrow as pa

    rng = random.Random(3)
    start = datetime(2026, 1, 1)
    happened = [start + timedelta(seconds=rng.randrange(120 * 86_400)) for _ in range(n_rows)]
    loaded = [h + timedelta(days=rng.choice([1, 1, 1, 2, 5, 8])) for h in happened]
    events = pa.table(
        {
            "user_id": pa.array([rng.randrange(300) for _ in range(n_rows)], pa.int64()),
            "event_ts": pa.array(happened, pa.timestamp("us")),
            "ingested_at": pa.array(loaded, pa.timestamp("us")),
        }
    )
    stamps = [start + timedelta(seconds=rng.randrange(120 * 86_400)) for _ in range(n_times)]
    labels = pa.table(
        {
            "user_id": pa.array([rng.randrange(300) for _ in stamps], pa.int64()),
            "label_ts": pa.array(stamps, pa.timestamp("us")),
        }
    )
    return events, labels, {s: s for s in sorted(set(stamps))}


def test_scoring_the_rows_in_flight_matches_a_brute_force_count():
    import pyarrow.compute as pc

    from sqlguard.leakage.replay import TAIL_EXPONENT, flight_scores

    events, labels, stamps = _synthetic_flight(3000, 60)
    got = flight_scores(events, "event_ts", "ingested_at", "user_id", labels, "label_ts", stamps)
    delay = pc.subtract(events["ingested_at"], events["event_ts"])
    longest = max(d.total_seconds() for d in delay.to_pylist())
    rows = events.to_pylist()
    for t, score in list(got.items())[:20]:
        users = {
            u
            for u, ts in zip(
                labels["user_id"].to_pylist(), labels["label_ts"].to_pylist(), strict=True
            )
            if ts == t
        }
        expect = sum(
            ((t - r["event_ts"]).total_seconds() // 1 / longest) ** TAIL_EXPONENT
            for r in rows
            if r["event_ts"] < t <= r["ingested_at"] and r["user_id"] in users
        )
        assert score == pytest.approx(expect, rel=1e-6)
    assert len(got) > 5


def test_scoring_the_rows_in_flight_stays_cheap_on_a_big_table():
    """100k rows and 3000 label times: set up once in well under the ten seconds a query session
    could notice (the scan it replaced was O(times x rows): 3.3 s here, hours at 1e7 rows)."""
    import time

    from sqlguard.leakage.replay import flight_scores

    events, labels, stamps = _synthetic_flight(100_000, 3000)
    started = time.perf_counter()
    scores = flight_scores(events, "event_ts", "ingested_at", "user_id", labels, "label_ts", stamps)
    elapsed = time.perf_counter() - started
    assert elapsed < 5.0, elapsed
    assert scores and all(w > 0 for w in scores.values())
    # more label times than the cap are thinned evenly, deterministically
    again = flight_scores(
        events, "event_ts", "ingested_at", "user_id", labels, "label_ts", stamps, max_times=100
    )
    assert len(again) <= 100 and again == flight_scores(
        events, "event_ts", "ingested_at", "user_id", labels, "label_ts", stamps, max_times=100
    )


# --- whatever the arrow types of the time columns -------------------------------------------


def _typed(events, labels, time_type=None, load_type=None):
    """The synthetic table with its columns cast to other temporal types."""
    import pyarrow as pa

    columns = {name: events[name] for name in events.column_names}
    if time_type is not None:
        columns["event_ts"] = events["event_ts"].cast(time_type)
    if load_type is not None:
        columns["ingested_at"] = events["ingested_at"].cast(load_type)
    return pa.table(columns)


def test_scores_do_not_depend_on_the_unit_or_zone_of_the_time_columns():
    """Mixed units used to leave every weight near 1e-28 (the delay was divided by the wrong
    unit); a time zone on one side used to raise."""
    import pyarrow as pa

    from sqlguard.leakage.replay import flight_scores

    events, labels, stamps = _synthetic_flight(3000, 60)
    base = flight_scores(events, "event_ts", "ingested_at", "user_id", labels, "label_ts", stamps)
    assert base and max(base.values()) > 1e-3
    for time_type, load_type in [
        (pa.timestamp("ns"), pa.timestamp("us")),
        (pa.timestamp("us"), pa.timestamp("ns")),
        (pa.timestamp("ms"), pa.timestamp("s")),
        (pa.timestamp("us", tz="UTC"), pa.timestamp("us")),
        (pa.timestamp("ns", tz="UTC"), pa.timestamp("ms", tz="UTC")),
    ]:
        typed = _typed(events, labels, time_type, load_type)
        got = flight_scores(typed, "event_ts", "ingested_at", "user_id", labels, "label_ts", stamps)
        # milliseconds and seconds drop the sub-unit part of a stamp: the ranking is the same
        assert set(got) >= set(base) - set(list(base)[:3]), (time_type, load_type)
        for t in list(base)[:10]:
            if t in got:
                assert got[t] == pytest.approx(base[t], rel=0.05), (time_type, load_type)


def test_a_date_time_column_is_scored_as_midnight():
    import pyarrow as pa

    from sqlguard.leakage.replay import flight_scores

    events, labels, stamps = _synthetic_flight(3000, 60)
    dated = _typed(events, labels, time_type=pa.date32())
    assert flight_scores(dated, "event_ts", "ingested_at", "user_id", labels, "label_ts", stamps)


def test_a_column_that_is_not_a_time_is_an_error_not_a_wrong_score():
    import pyarrow as pa

    from sqlguard.leakage.replay import flight_scores

    events, labels, stamps = _synthetic_flight(100, 10)
    broken = _typed(events, labels).set_column(1, "event_ts", pa.array(["x"] * 100))
    with pytest.raises(TypeError, match="as a time"):
        flight_scores(broken, "event_ts", "ingested_at", "user_id", labels, "label_ts", stamps)


def test_a_failing_in_flight_score_degrades_to_the_base_sample_and_says_so(world):
    catalog, _ = world

    class Failing(Replayer):
        def _flight_scores(self, *args, **kwargs):
            raise AttributeError("no unit")

    broken, plain = Failing(catalog, SPEC, seed=42), Replayer(catalog, SPEC, seed=42)
    result = broken.run(BOUNDED)
    assert result.status is ReplayStatus.PASS
    assert "in-flight sampling skipped for 'events': AttributeError: no unit" in result.reason
    assert result.points_checked < plain.run(BOUNDED).points_checked  # the base sample only
