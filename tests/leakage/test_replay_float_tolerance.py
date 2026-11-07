"""Replay compares floats to a relative tolerance plus a small absolute one that grows with the
column, so the rounding noise of a difference of nearly equal sums is not a mismatch, while a
real difference of 1e-6 on a value near 1 still is."""

import math
from datetime import datetime, timedelta

import pyarrow as pa
import pytest

from sqlguard.core.catalog import LOGICAL_TS_PROPERTY, LakehouseCatalog
from sqlguard.leakage import replay as replay_module
from sqlguard.leakage.replay import Replayer, ReplayStatus
from sqlguard.leakage.spec import LeakageSpec

START = datetime(2026, 1, 1)
USERS, DAYS, FIRST_LABEL_DAY, REWRITE_DAY = 12, 9, 3, 5
PARTS = (0.1, 0.2, 0.3)  # (0.1 + 0.2) + 0.3 is not 0.3 + (0.2 + 0.1) in floating point
TINY = 1e-6


# --- the comparison itself ---------------------------------------------------------------------


def test_the_order_of_addition_changes_the_last_bits_of_a_difference():
    forward = (PARTS[0] + PARTS[1] + PARTS[2]) - 0.6
    backward = (PARTS[2] + PARTS[1] + PARTS[0]) - 0.6
    assert forward != backward and abs(forward - backward) < 1e-15
    tolerances = [replay_module._ABS_FLOOR]
    assert replay_module._same_rows([(forward,)], [(backward,)], tolerances)


@pytest.mark.parametrize(
    "a, b, tolerance, same",
    [
        (1.0, 1.0 + 1e-6, 1e-12, False),  # a real difference of 1e-6 on a value near 1
        (1.0, 1.0 + 1e-8, 1e-12, False),
        (1.0, 1.0 + 1e-12, 1e-12, True),  # within the relative tolerance
        (0.0, 3e-13, 1e-12, True),  # noise around zero
        (0.0, 3e-9, 1e-12, False),
        (0.0, 3e-9, 1e-8, True),  # a column that holds values of 10 can absorb more
        (float("inf"), float("inf"), 1e-12, True),
        (float("inf"), 1e308, 1e-12, False),
        (None, 0.0, 1e-12, False),
        (True, 1.0000000001, 1e-12, False),
        (3, 3, 0.0, True),
        (3, 4, 0.0, False),
        ("nan", "nan", 0.0, True),
    ],
)
def test_close_values(a, b, tolerance, same):
    assert replay_module._close(a, b, tolerance) is same


def test_rows_that_float_noise_ordered_differently_still_match():
    now = [(1.0000000001, 5.0), (1.0000000002, 6.0)]
    past = [(1.0000000002, 6.0), (1.0000000001, 5.0)]
    assert replay_module._same_rows(now, past, [0.0, 0.0])


def test_rows_of_other_values_do_not_match():
    assert not replay_module._same_rows([(1.0,), (2.0,)], [(1.0,), (2.5,)], [1e-12])
    assert not replay_module._same_rows([(1.0,)], [(1.0,), (1.0,)], [1e-12])


def test_only_float_columns_get_an_absolute_tolerance():
    table = pa.table(
        {
            "small": pa.array([1e-4, -2e-4, 3e-4], pa.float64()),
            "large": pa.array([1e6, -1e6, 1e6], pa.float64()),
            "count": pa.array([1, 2, 3], pa.int64()),
            "text": pa.array(["a", "b", "c"]),
        }
    )
    got = replay_module._tolerances(table, ["small", "large", "count", "text"])
    assert got == [replay_module._ABS_FLOOR, replay_module._ABS_FLOOR, 0.0, 0.0]


def test_a_big_value_beside_a_small_one_does_not_excuse_a_difference_in_the_small_one():
    # the tolerance is the same for every value of a column: 0.5 against 0.9 is a difference
    # whatever else the column holds
    (tolerance,) = replay_module._tolerances(
        pa.table({"x": pa.array([1e9] * 11 + [0.5], pa.float64())}), ["x"]
    )
    assert not replay_module._close(0.5, 0.9, tolerance)
    assert not replay_module._close(0.5, 0.5 + 1e-6, tolerance)
    assert replay_module._close(1e9, 1e9 * (1 + 1e-10), tolerance)


# --- replay over a table that was rewritten in another row order ---------------------------------


def _ledger(extra: bool, reverse: bool) -> pa.Table:
    rows = []
    for user in range(1, USERS + 1):
        parts = [(user, START + timedelta(hours=1 + i), amount) for i, amount in enumerate(PARTS)]
        rows += reversed(parts) if reverse else parts
        if extra:
            rows.append((user, START + timedelta(days=REWRITE_DAY, hours=1), TINY))
    users, moments, amounts = zip(*rows, strict=True)
    return pa.table(
        {
            "user_id": pa.array(users, pa.int64()),
            "event_ts": pa.array(moments, pa.timestamp("us")),
            "amount": pa.array(amounts, pa.float64()),
        }
    )


def _labels() -> pa.Table:
    rows = [
        (u, START + timedelta(days=d, hours=12), u % 2)
        for u in range(1, USERS + 1)
        for d in range(FIRST_LABEL_DAY, DAYS)
    ]
    users, moments, churned = zip(*rows, strict=True)
    return pa.table(
        {
            "user_id": pa.array(users, pa.int64()),
            "label_ts": pa.array(moments, pa.timestamp("us")),
            "churned": pa.array(churned, pa.int64()),
        }
    )


@pytest.fixture(scope="module")
def catalog(tmp_path_factory):
    """`ledger` is loaded in one order on day 0 and rewritten on day 5 in the opposite order, with
    a tiny new fact for each user. The facts of day 0 are summed in one order from the snapshot of
    the days before the rewrite and in the other order from today's table."""
    built = LakehouseCatalog.local(tmp_path_factory.mktemp("ledger"))
    built.create_table("labels", _labels())
    first = _ledger(extra=False, reverse=False)
    built.create_empty("ledger", first.schema)
    table = built.table("ledger")
    table.append(first, snapshot_properties={LOGICAL_TS_PROPERTY: START.isoformat()})
    table.overwrite(
        _ledger(extra=True, reverse=True),
        snapshot_properties={
            LOGICAL_TS_PROPERTY: (START + timedelta(days=REWRITE_DAY, hours=2)).isoformat()
        },
    )
    return built


def _spec() -> LeakageSpec:
    return LeakageSpec.from_dict(
        {
            "label_table": "labels",
            "entity_keys": ["user_id"],
            "prediction_ts": "label_ts",
            "label_columns": ["churned"],
            "time_columns": {"ledger": "event_ts"},
            "static_tables": [],
        }
    )


RECONCILE = """SELECT l.user_id, l.label_ts, l.churned,
  COALESCE(SUM(g.amount), 0) - 0.6 AS unexplained
FROM labels l LEFT JOIN ledger g ON g.user_id = l.user_id AND g.event_ts < {bound}
GROUP BY l.user_id, l.label_ts, l.churned"""


# the facts of day 0 only: the same set of rows at every prediction time, summed in the order the
# table holds them, which is not the order of the snapshot the replay reads
OLD_FACTS = """WITH s AS (
  SELECT user_id, SUM(amount) AS total FROM ledger
  WHERE event_ts < TIMESTAMP '2026-01-02' GROUP BY user_id)
SELECT l.user_id, l.label_ts, l.churned, s.total - 0.6 AS unexplained
FROM labels l LEFT JOIN s ON s.user_id = l.user_id"""


def test_noise_in_the_difference_of_two_sums_is_not_a_mismatch(catalog):
    result = Replayer(catalog, _spec()).run(OLD_FACTS)
    assert result.status is ReplayStatus.PASS, result.reason
    assert result.rows_compared >= 20


def test_a_fact_that_was_not_loaded_yet_still_shows_as_a_mismatch(catalog):
    # the bound reaches a day past the prediction time: the 1e-6 fact of day 5 is counted at the
    # days before it was loaded
    result = Replayer(catalog, _spec()).run(RECONCILE.format(bound="l.label_ts + INTERVAL 1 DAY"))
    assert result.status is ReplayStatus.MISMATCH
    assert result.mismatched_columns == {"unexplained": result.rows_mismatched}


def test_a_mismatch_of_one_millionth_on_a_value_near_one_is_reported(catalog):
    bound = "l.label_ts + INTERVAL 1 DAY"
    result = Replayer(catalog, _spec()).run(
        f"""SELECT l.user_id, l.label_ts, l.churned, COALESCE(SUM(g.amount), 0) AS total
FROM labels l LEFT JOIN ledger g ON g.user_id = l.user_id AND g.event_ts < {bound}
GROUP BY l.user_id, l.label_ts, l.churned"""
    )
    assert result.status is ReplayStatus.MISMATCH
    assert math.isclose(TINY, 1e-6)
