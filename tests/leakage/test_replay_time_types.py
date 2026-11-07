"""Point-in-time replay for the time semantics a spec can declare: a label table that is also
event history, a single series (no entity key), labels final the moment they are stamped, and
prediction times that are DATEs or integers with a declared unit.

The world is small and exact. Events of day d happen at 01:00 and are loaded at 02:00; labels
are stamped at 12:00 or later, so a bound on event time `< label_ts` is exactly what was loaded.
"""

from datetime import date, datetime, timedelta

import pyarrow as pa
import pytest

from sqlguard.core.catalog import LOGICAL_TS_PROPERTY, LakehouseCatalog
from sqlguard.leakage.replay import Replayer, ReplayStatus
from sqlguard.leakage.spec import LeakageSpec

EPOCH = datetime(1970, 1, 1)
START = datetime(2026, 1, 1)
USERS, DAYS, FIRST_LABEL_DAY = 12, 9, 3
KINDS = ("timestamp", "date", "seconds", "milliseconds")
TYPES = {
    "timestamp": pa.timestamp("us"),
    "date": pa.date32(),
    "seconds": pa.int64(),
    "milliseconds": pa.int64(),
}


def stamp(kind: str, moment: datetime):
    """`moment` in the representation of a time column of this kind."""
    if kind == "timestamp":
        return moment
    if kind == "date":
        return moment.date()
    seconds = int((moment - EPOCH).total_seconds())
    return seconds if kind == "seconds" else seconds * 1000


def _array(kind: str, moments: list[datetime]) -> pa.Array:
    return pa.array([stamp(kind, m) for m in moments], TYPES[kind])


def labels_table(kind: str, first_day: int = FIRST_LABEL_DAY) -> pa.Table:
    rows = [
        (u, START + timedelta(days=d, hours=12 + u % 5), u % 2)
        for u in range(1, USERS + 1)
        for d in range(first_day, DAYS)
    ]
    users, moments, churned = zip(*rows, strict=True)
    return pa.table(
        {
            "user_id": pa.array(users, pa.int64()),
            "label_ts": _array(kind, list(moments)),
            "churned": pa.array(churned, pa.int64()),
        }
    )


def events_table(kind: str) -> pa.Table:
    rows = [
        (u * 100 + d, u, START + timedelta(days=d, hours=1))
        for u in range(1, USERS + 1)
        for d in range(DAYS)
    ]
    ids, users, moments = zip(*rows, strict=True)
    return pa.table(
        {
            "event_id": pa.array(ids, pa.int64()),
            "user_id": pa.array(users, pa.int64()),
            "event_ts": _array(kind, list(moments)),
            "amount": pa.array([5.0] * len(rows), pa.float64()),
        }
    )


def build_catalog(path, kind: str, *, labels: pa.Table | None = None) -> LakehouseCatalog:
    """Labels loaded in one commit; events committed a day at a time, each loaded at 02:00."""
    catalog = LakehouseCatalog.local(path)
    catalog.create_table("labels", labels if labels is not None else labels_table(kind))
    events = events_table(kind)
    catalog.create_empty("events", events.schema)
    day_of = events.column("event_id").to_pylist()
    for d in range(DAYS):
        rows = [i for i, event_id in enumerate(day_of) if event_id % 100 == d]
        catalog.table("events").append(
            events.take(rows),
            snapshot_properties={
                LOGICAL_TS_PROPERTY: (START + timedelta(days=d, hours=2)).isoformat()
            },
        )
    return catalog


def spec_for(kind: str, **extra) -> LeakageSpec:
    units = {"seconds": "seconds", "milliseconds": "milliseconds"}
    declared = (
        {"time_units": {"labels": units[kind], "events": units[kind]}} if kind in units else {}
    )
    return LeakageSpec.from_dict(
        {
            "label_table": "labels",
            "entity_keys": ["user_id"],
            "prediction_ts": "label_ts",
            "label_columns": ["churned"],
            "time_columns": {"labels": "label_ts", "events": "event_ts"},
            "static_tables": [],
            **declared,
            **extra,
        }
    )


ONE_DAY = {
    "timestamp": "INTERVAL 1 DAY",
    "date": "1",
    "seconds": "86400",
    "milliseconds": "86400000",
}
COUNT = """SELECT l.user_id, l.label_ts, COUNT(e.event_id) AS n
FROM labels l LEFT JOIN events e ON e.user_id = l.user_id AND {bound}
GROUP BY l.user_id, l.label_ts"""


def count_before(kind: str, leak: bool) -> str:
    bound = f"e.event_ts <= l.label_ts + {ONE_DAY[kind]}" if leak else "e.event_ts < l.label_ts"
    return COUNT.format(bound=bound)


@pytest.fixture(scope="module")
def worlds(tmp_path_factory):
    return {k: build_catalog(tmp_path_factory.mktemp(k), k) for k in KINDS}


# --- a label table that is also event history -----------------------------------------------

VISITS = 12
LATE = timedelta(days=2)


def visits_table() -> pa.Table:
    """One visit per visitor per day at 12:00, loaded an hour later; every third day's visits
    arrive two days late. Returns the rows and, per row, when it was loaded."""
    rows = [
        (
            v,
            START + timedelta(days=d, hours=12),
            START + timedelta(days=d, hours=13) + (LATE if d % 3 == 0 else timedelta()),
            v % 2,
        )
        for v in range(1, VISITS + 1)
        for d in range(DAYS)
    ]
    visitors, moments, loaded, converted = zip(*rows, strict=True)
    return pa.table(
        {
            "visitor_id": pa.array(visitors, pa.int64()),
            "visit_ts": pa.array(moments, pa.timestamp("us")),
            "loaded_at": pa.array(loaded, pa.timestamp("us")),
            "converted": pa.array(converted, pa.int64()),
            "revenue": pa.array([1.0] * len(rows), pa.float64()),
        }
    )


def build_visits(path, *, progressive: bool) -> LakehouseCatalog:
    """The visits table committed once (as a seed), or one commit per load time."""
    catalog = LakehouseCatalog.local(path)
    visits = visits_table()
    if not progressive:
        catalog.create_table("visits", visits)
        return catalog
    catalog.create_empty("visits", visits.schema)
    loaded = visits.column("loaded_at").to_pylist()
    for moment in sorted(set(loaded)):
        catalog.table("visits").append(
            visits.take([i for i, value in enumerate(loaded) if value == moment]),
            snapshot_properties={LOGICAL_TS_PROPERTY: moment.isoformat()},
        )
    return catalog


def visits_spec(**extra) -> LeakageSpec:
    return LeakageSpec.from_dict(
        {
            "label_table": "visits",
            "entity_keys": ["visitor_id"],
            "prediction_ts": "visit_ts",
            "label_columns": ["converted"],
            "time_columns": {"visits": "visit_ts"},
            "availability_columns": {"visits": "loaded_at"},
            "static_tables": [],
            **extra,
        }
    )


PRIOR_VISITS = """SELECT l.visitor_id, l.visit_ts,
  (SELECT COUNT(*) FROM visits h WHERE h.visitor_id = l.visitor_id AND {bound}) AS prior
FROM visits l"""


@pytest.fixture(scope="module")
def progressive(tmp_path_factory):
    return build_visits(tmp_path_factory.mktemp("progressive"), progressive=True)


@pytest.fixture(scope="module")
def seeded(tmp_path_factory):
    return build_visits(tmp_path_factory.mktemp("seeded"), progressive=False)


def test_history_of_a_label_table_is_read_as_it_was_loaded(progressive):
    # the visits of days 0, 3 and 6 reach the table two days after they happened: a count
    # bounded on event time alone sees them in today's data and not in the snapshot of the day
    replay = Replayer(progressive, visits_spec()).run(
        PRIOR_VISITS.format(bound="h.visit_ts < l.visit_ts")
    )
    assert replay.status is ReplayStatus.MISMATCH
    assert replay.mismatched_columns == {"prior": replay.rows_mismatched}


def test_history_bounded_on_load_time_replays_clean(progressive):
    replay = Replayer(progressive, visits_spec()).run(
        PRIOR_VISITS.format(bound="h.loaded_at < l.visit_ts")
    )
    assert replay.status is ReplayStatus.PASS, replay.reason


def test_a_label_table_loaded_in_one_commit_keeps_its_history_by_prediction_time(seeded):
    # the only snapshot comes after every prediction time: there is no history to read, so the
    # earlier rows are the ones stamped before the prediction time
    replay = Replayer(seeded, visits_spec()).run(
        PRIOR_VISITS.format(bound="h.visit_ts < l.visit_ts")
    )
    assert replay.status is ReplayStatus.PASS, replay.reason


def test_a_label_table_not_listed_as_temporal_is_not_read_from_snapshots(progressive):
    spec = LeakageSpec.from_dict(
        {
            "label_table": "visits",
            "entity_keys": ["visitor_id"],
            "prediction_ts": "visit_ts",
            "label_columns": ["converted"],
            "time_columns": {"other": "x"},
            "static_tables": [],
        }
    )
    replay = Replayer(progressive, spec).run(PRIOR_VISITS.format(bound="h.visit_ts < l.visit_ts"))
    assert replay.status is ReplayStatus.PASS, replay.reason


# --- an outcome column in a table that is not only an outcome ---------------------------------


def test_a_temporal_table_with_an_outcome_column_is_replayed_like_any_temporal_table(tmp_path):
    """Only the outcome column is the target; the table keeps its history, so a prediction time
    before its first snapshot is left out instead of replayed against an empty table."""
    early = pa.concat_tables(
        [
            pa.table(
                {
                    "user_id": pa.array(range(1, USERS + 1), pa.int64()),
                    "label_ts": _array("timestamp", [START] * USERS),
                    "churned": pa.array([0] * USERS, pa.int64()),
                }
            ),
            labels_table("timestamp"),
        ]
    )
    catalog = build_catalog(tmp_path, "timestamp", labels=early)
    sql = count_before("timestamp", leak=False)
    plain = Replayer(catalog, spec_for("timestamp")).run(sql)
    declared = Replayer(catalog, spec_for("timestamp", outcome_columns=["events.amount"])).run(sql)
    assert "before the start of history" in plain.reason
    assert declared.status is plain.status is ReplayStatus.PASS
    assert declared.reason == plain.reason
    assert declared.rows_compared == plain.rows_compared


# --- labels that are final the moment they are stamped ----------------------------------------


def plays_table() -> pa.Table:
    rows = [
        (u, START + timedelta(days=d, hours=12), (u + d) % 2)
        for u in range(1, USERS + 1)
        for d in range(DAYS)
    ]
    users, moments, correct = zip(*rows, strict=True)
    return pa.table(
        {
            "user_id": pa.array(users, pa.int64()),
            "ts": pa.array(moments, pa.timestamp("us")),
            "correct": pa.array(correct, pa.int64()),
        }
    )


def plays_spec(**extra) -> LeakageSpec:
    return LeakageSpec.from_dict(
        {
            "label_table": "plays",
            "entity_keys": ["user_id"],
            "prediction_ts": "ts",
            "label_columns": ["correct"],
            "time_columns": {"plays": "ts"},
            "static_tables": [],
            **extra,
        }
    )


PRIOR_RATE = """SELECT l.user_id, l.ts, l.correct,
  (SELECT AVG(h.correct) FROM plays h WHERE h.user_id = l.user_id AND h.ts < l.ts) AS prior_rate
FROM plays l"""


@pytest.fixture(scope="module")
def plays(tmp_path_factory):
    catalog = LakehouseCatalog.local(tmp_path_factory.mktemp("plays"))
    catalog.create_table("plays", plays_table())
    return catalog


def test_labels_known_at_once_stay_visible_to_the_rows_after_them(plays):
    replay = Replayer(plays, plays_spec(label_known_after="0 seconds")).run(PRIOR_RATE)
    assert replay.status is ReplayStatus.PASS, replay.reason
    assert replay.rows_compared == USERS * DAYS


def test_a_delay_written_as_the_number_zero_is_the_same_as_zero_seconds(plays):
    replay = Replayer(plays, plays_spec(label_known_after=0)).run(PRIOR_RATE)
    assert replay.status is ReplayStatus.PASS, replay.reason


def test_without_a_declared_delay_the_earlier_labels_are_hidden(plays):
    replay = Replayer(plays, plays_spec()).run(PRIOR_RATE)
    assert replay.status is ReplayStatus.MISMATCH
    assert set(replay.mismatched_columns) == {"prior_rate"}


def test_a_positive_delay_hides_the_labels_stamped_inside_it(plays):
    replay = Replayer(plays, plays_spec(label_known_after="2 days")).run(PRIOR_RATE)
    assert replay.status is ReplayStatus.MISMATCH
    assert set(replay.mismatched_columns) == {"prior_rate"}


# --- a single series: no entity key ------------------------------------------------------------


def series_tables() -> tuple[pa.Table, pa.Table]:
    """Hourly points from 12:00 to 23:00 on days 3 to 8; one event a day, loaded an hour later."""
    moments = [
        START + timedelta(days=d, hours=h)
        for d in range(FIRST_LABEL_DAY, DAYS)
        for h in range(12, 24)
    ]
    series = pa.table(
        {
            "label_ts": pa.array(moments, pa.timestamp("us")),
            "y": pa.array([i % 3 for i in range(len(moments))], pa.int64()),
        }
    )
    happened = [START + timedelta(days=d, hours=1) for d in range(DAYS)]
    events = pa.table(
        {
            "event_ts": pa.array(happened, pa.timestamp("us")),
            "ingested_at": pa.array([m + timedelta(hours=1) for m in happened], pa.timestamp("us")),
            "amount": pa.array([5.0] * DAYS, pa.float64()),
        }
    )
    return series, events


SERIES = """SELECT l.label_ts, l.y, SUM(e.amount) AS total
FROM series l LEFT JOIN events e ON {bound} GROUP BY l.label_ts, l.y"""


@pytest.fixture(scope="module")
def single_series(tmp_path_factory):
    catalog = LakehouseCatalog.local(tmp_path_factory.mktemp("series"))
    series, events = series_tables()
    catalog.create_table("series", series)
    catalog.create_empty("events", events.schema)
    for i in range(DAYS):
        catalog.table("events").append(
            events.slice(i, 1),
            snapshot_properties={
                LOGICAL_TS_PROPERTY: events.column("ingested_at")[i].as_py().isoformat()
            },
        )
    return catalog


def series_spec() -> LeakageSpec:
    return LeakageSpec.from_dict(
        {
            "label_table": "series",
            "entity_keys": [],
            "prediction_ts": "label_ts",
            "label_columns": ["y"],
            "time_columns": {"events": "event_ts"},
            "availability_columns": {"events": "ingested_at"},
            "static_tables": [],
        }
    )


def test_a_series_without_entity_keys_replays_clean_and_counts_what_it_hid(single_series):
    replay = Replayer(single_series, series_spec()).run(
        SERIES.format(bound="e.event_ts < l.label_ts")
    )
    assert replay.status is ReplayStatus.PASS, replay.reason
    assert replay.exposure["events"] > 0  # the whole table counts: there is no entity to narrow to
    assert replay.exposed_points["events"] > 0


def test_a_series_without_entity_keys_still_catches_a_read_of_later_rows(single_series):
    replay = Replayer(single_series, series_spec()).run(
        SERIES.format(bound="e.event_ts <= l.label_ts + INTERVAL 1 DAY")
    )
    assert replay.status is ReplayStatus.MISMATCH
    assert set(replay.mismatched_columns) == {"total"}


def test_rows_in_flight_are_scored_over_the_whole_table_when_there_is_no_key():
    from sqlguard.core.catalog import naive_utc
    from sqlguard.leakage.replay import flight_scores

    happened = [START + timedelta(days=d, hours=11, minutes=30) for d in range(DAYS)]
    events = pa.table(
        {
            "event_ts": pa.array(happened, pa.timestamp("us")),
            "ingested_at": pa.array([m + timedelta(hours=1) for m in happened], pa.timestamp("us")),
        }
    )
    series, _ = series_tables()
    stamps = {naive_utc(t): t for t in series.column("label_ts").to_pylist()}
    scores = flight_scores(events, "event_ts", "ingested_at", None, series, "label_ts", stamps)
    # the event of day d is in flight at 12:00 of that day (happened, not yet loaded) and at no
    # other label time
    assert set(scores) == {
        START + timedelta(days=d, hours=12) for d in range(FIRST_LABEL_DAY, DAYS)
    }
