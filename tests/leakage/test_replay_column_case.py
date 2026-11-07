"""Replay on tables whose column names are not lowercase.

A spec stores every name lowercase (it matches unquoted SQL identifiers), while an Iceberg table
keeps the name it was created with (`visitStartTime`, `TransactionDT`). Each read of a table by a
spec name must resolve it without regard to case, and give the same answer as the lowercase twin.
"""

from datetime import datetime, timedelta

import pyarrow as pa
import pyarrow.compute as pc
import pytest

from sqlguard.core.catalog import LOGICAL_TS_PROPERTY, LakehouseCatalog
from sqlguard.leakage.replay import Replayer, ReplayStatus
from sqlguard.leakage.spec import LeakageSpec

START = datetime(2026, 1, 1)
USERS, DAYS = 12, 9

# lowercase name -> the name the table carries
MIXED = {
    "user_id": "UserId",
    "label_ts": "LabelTs",
    "churned": "Churned",
    "event_id": "EventId",
    "event_ts": "EventTs",
    "ingested_at": "IngestedAt",
    "amount": "Amount",
}
LOWER = {name: name for name in MIXED}

SPEC = LeakageSpec.from_dict(
    {
        "label_table": "labels",
        "entity_keys": ["UserId"],
        "prediction_ts": "LabelTs",
        "label_columns": ["Churned"],
        "time_columns": {"events": "EventTs"},
        "availability_columns": {"events": "IngestedAt"},
        "static_tables": [],
    }
)


def _labels(names: dict[str, str]) -> pa.Table:
    rows = [
        (u, START + timedelta(days=3 + d, hours=u % 5), u % 2)
        for u in range(1, USERS + 1)
        for d in range(DAYS - 3)
    ]
    cols = list(zip(*rows, strict=True))
    return pa.table(
        {
            names["user_id"]: pa.array(cols[0], pa.int64()),
            names["label_ts"]: pa.array(cols[1], pa.timestamp("us")),
            names["churned"]: pa.array(cols[2], pa.int64()),
        }
    )


def _events(names: dict[str, str]) -> pa.Table:
    rows = [
        (u * 100 + d, u, START + timedelta(days=d, hours=1), START + timedelta(days=d, hours=2))
        for u in range(1, USERS + 1)
        for d in range(DAYS)
    ]
    cols = list(zip(*rows, strict=True))
    return pa.table(
        {
            names["event_id"]: pa.array(cols[0], pa.int64()),
            names["user_id"]: pa.array(cols[1], pa.int64()),
            names["event_ts"]: pa.array(cols[2], pa.timestamp("us")),
            names["ingested_at"]: pa.array(cols[3], pa.timestamp("us")),
            names["amount"]: pa.array([5.0] * len(rows), pa.float64()),
        }
    )


def _catalog(path, names: dict[str, str]) -> LakehouseCatalog:
    """Labels loaded once; events committed one day at a time, each readable when ingested."""
    catalog = LakehouseCatalog.local(path)
    catalog.create_table("labels", _labels(names))
    events = _events(names)
    catalog.create_empty("events", events.schema)
    loaded = events[names["ingested_at"]]
    for d in range(DAYS):
        day = events.filter(
            pc.equal(
                pc.floor_temporal(loaded, unit="day"),
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


@pytest.fixture(scope="module")
def mixed(tmp_path_factory):
    return _catalog(tmp_path_factory.mktemp("mixed"), MIXED)


@pytest.fixture(scope="module")
def lower(tmp_path_factory):
    return _catalog(tmp_path_factory.mktemp("lower"), LOWER)


def _sql(names: dict[str, str], when: str) -> str:
    n = names
    return (
        f"SELECT l.{n['user_id']}, l.{n['label_ts']}, l.{n['churned']}, "
        f"SUM(e.{n['amount']}) AS amt "
        f"FROM labels l LEFT JOIN events e ON e.{n['user_id']} = l.{n['user_id']} "
        f"AND e.{n[when]} < l.{n['label_ts']} GROUP BY 1, 2, 3"
    )


def _prior_label(names: dict[str, str]) -> str:
    n = names
    return (
        f"SELECT {n['user_id']}, {n['label_ts']}, "
        f"LAG({n['churned']}) OVER (PARTITION BY {n['user_id']} ORDER BY {n['label_ts']}) AS prev "
        "FROM labels"
    )


def test_a_label_table_with_mixed_case_columns_opens(mixed):
    replayer = Replayer(mixed, SPEC)
    assert replayer.points


def test_a_clean_query_passes_and_a_late_data_query_diverges_as_on_lowercase_columns(mixed, lower):
    spec_lower = LeakageSpec.from_dict(
        {
            "label_table": "labels",
            "entity_keys": ["user_id"],
            "prediction_ts": "label_ts",
            "label_columns": ["churned"],
            "time_columns": {"events": "event_ts"},
            "availability_columns": {"events": "ingested_at"},
            "static_tables": [],
        }
    )
    for when, status in [("ingested_at", ReplayStatus.PASS), ("event_ts", ReplayStatus.MISMATCH)]:
        got = Replayer(mixed, SPEC).run(_sql(MIXED, when))
        want = Replayer(lower, spec_lower).run(_sql(LOWER, when))
        assert got.status is want.status is status, (when, got.reason)
        assert (got.rows_compared, got.rows_mismatched) == (
            want.rows_compared,
            want.rows_mismatched,
        )
        assert got.mismatched_columns == want.mismatched_columns
        assert got.exposure == want.exposure and got.exposed_points == want.exposed_points


def test_earlier_labels_stay_hidden_when_the_label_column_has_capitals(mixed):
    """A feature that reads an earlier row's label must not replay identically."""
    result = Replayer(mixed, SPEC).run(_prior_label(MIXED))
    assert result.status is ReplayStatus.MISMATCH
    assert set(result.mismatched_columns) == {"prev"}


def test_rows_in_flight_are_scored_for_capitalised_time_and_availability_columns(mixed, lower):
    replayer = Replayer(mixed, SPEC)
    scores = replayer._in_flight("events")
    assert not replayer._flight_errors
    assert scores
    assert set(scores) == set(Replayer(lower, _lower_spec())._in_flight("events"))


def _lower_spec() -> LeakageSpec:
    return LeakageSpec.from_dict(
        {
            "label_table": "labels",
            "entity_keys": ["user_id"],
            "prediction_ts": "label_ts",
            "label_columns": ["churned"],
            "time_columns": {"events": "event_ts"},
            "availability_columns": {"events": "ingested_at"},
            "static_tables": [],
        }
    )


def test_column_lookup_ignores_case_and_leaves_an_absent_name_alone():
    from sqlguard.leakage.replay import _column

    table = pa.table({"visitStartTime": [1], "Other": [2]})
    assert _column(table, "visitstarttime") == "visitStartTime"
    assert _column(table, "visitStartTime") == "visitStartTime"
    assert _column(table, "missing") == "missing"
    with pytest.raises(KeyError):
        table.column(_column(table, "missing"))


def test_two_columns_that_differ_only_in_case_are_an_error_not_a_guess():
    from sqlguard.leakage.replay import ColumnError, _column

    table = pa.table({"Ts": [1], "ts": [2]})
    with pytest.raises(ColumnError, match="Ts.*ts|ts.*Ts"):
        _column(table, "ts")
