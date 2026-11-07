"""A number in a declared unit, scaled or turned into a timestamp, bounds another the same way.

The spine counts milliseconds and a price table counts seconds. Written as a product, a quotient
or `to_timestamp` / `epoch_ms` / `make_timestamp`, a lookup two days back is as safely bounded as
one in a single unit. A conversion the declared units do not support bounds nothing.
"""

import pytest

from sqlguard.leakage.spec import LeakageSpec
from sqlguard.leakage.validator import static_findings

SPEC = {
    "label_table": "interactions",
    "entity_keys": ["user_id"],
    "prediction_ts": "ts_ms",
    "label_columns": ["correct"],
    "time_columns": {"interactions": "ts_ms", "market_prices": "ts_s"},
    "time_units": {"interactions": "milliseconds", "market_prices": "seconds"},
    "label_known_after": "0 seconds",
}
SCHEMA = {
    "interactions": {"user_id": "BIGINT", "ts_ms": "BIGINT", "correct": "BIGINT", "x": "DOUBLE"},
    "market_prices": {"user_id": "BIGINT", "ts_s": "BIGINT", "price": "DOUBLE"},
}
LOOKUP = """SELECT s.user_id, s.ts_ms, s.correct, AVG(p.price) AS avg_price
FROM interactions s LEFT JOIN market_prices p ON p.user_id = s.user_id AND {bound}
GROUP BY ALL"""
TWO_DAYS_S, TWO_DAYS_MS = 172_800, 172_800_000


def rules(bound: str, **overrides) -> set[str]:
    spec = LeakageSpec.from_dict({**SPEC, **overrides})
    return {f.rule_id for f in static_findings(LOOKUP.format(bound=bound), spec, SCHEMA)}


SAFE = {
    "product": f"p.ts_s * 1000 < s.ts_ms - {TWO_DAYS_MS}",
    "quotient": f"p.ts_s < s.ts_ms / 1000 - {TWO_DAYS_S}",
    "integer quotient": f"p.ts_s < s.ts_ms // 1000 - {TWO_DAYS_S}",
    "product on the left": f"1000 * p.ts_s < s.ts_ms - {TWO_DAYS_MS}",
    "to_timestamp": "to_timestamp(p.ts_s) < to_timestamp(s.ts_ms / 1000) - INTERVAL 2 DAY",
    "epoch_ms": "epoch_ms(p.ts_s * 1000) < epoch_ms(s.ts_ms) - INTERVAL 2 DAY",
    "make_timestamp": (
        "make_timestamp(p.ts_s * 1000000) < make_timestamp(s.ts_ms * 1000) - INTERVAL 2 DAY"
    ),
    "reversed": f"s.ts_ms - {TWO_DAYS_MS} > p.ts_s * 1000",
    "pending in the converted unit": (
        f"to_timestamp(p.ts_s) < to_timestamp(s.ts_ms / 1000 - {TWO_DAYS_S})"
    ),
}


@pytest.mark.parametrize("name", sorted(SAFE))
def test_a_lookup_two_days_back_across_two_units_is_bounded(name):
    assert rules(SAFE[name]) == set(), name


# the same spellings with a bound that is wrong, or a conversion the units do not support
WRONG = {
    "no conversion": f"p.ts_s < s.ts_ms - {TWO_DAYS_S}",
    "bare milliseconds against seconds": f"p.ts_s < s.ts_ms - {TWO_DAYS_MS}",
    "product, two days ahead": f"p.ts_s * 1000 < s.ts_ms + {TWO_DAYS_MS}",
    "quotient, two days ahead": f"p.ts_s < s.ts_ms / 1000 + {TWO_DAYS_S}",
    "wrong factor": f"p.ts_s * 100 < s.ts_ms - {TWO_DAYS_MS}",
    "wrong divisor": f"p.ts_s < s.ts_ms / 100 - {TWO_DAYS_S}",
    "equal instant": "p.ts_s * 1000 <= s.ts_ms",
    "a lower bound only": f"p.ts_s * 1000 > s.ts_ms - {TWO_DAYS_MS}",
    "to_timestamp of milliseconds": "to_timestamp(p.ts_s) < to_timestamp(s.ts_ms) - INTERVAL 2 DAY",
    "epoch_ms of seconds": "epoch_ms(p.ts_s) < epoch_ms(s.ts_ms) - INTERVAL 2 DAY",
    "make_timestamp of milliseconds": (
        "make_timestamp(p.ts_s * 1000000) < make_timestamp(s.ts_ms) - INTERVAL 2 DAY"
    ),
    "ahead by interval": "to_timestamp(p.ts_s) < to_timestamp(s.ts_ms / 1000) + INTERVAL 2 DAY",
    "two kinds of timestamp": ("to_timestamp(p.ts_s) < epoch_ms(s.ts_ms) - INTERVAL 2 DAY"),
    "a number against a timestamp": "to_timestamp(p.ts_s) < s.ts_ms - 5",
    "a number added to a timestamp": "to_timestamp(p.ts_s) + 5 < to_timestamp(s.ts_ms / 1000)",
    "a fraction of an unit": f"p.ts_s * 0.5 < s.ts_ms - {TWO_DAYS_MS}",
}


@pytest.mark.parametrize("name", sorted(WRONG))
def test_a_wrong_conversion_or_bound_is_still_flagged(name):
    assert rules(WRONG[name]), name


@pytest.mark.parametrize("missing", ["interactions", "market_prices"])
def test_a_table_without_a_declared_unit_is_not_converted(missing):
    units = {t: u for t, u in SPEC["time_units"].items() if t != missing}
    for bound in SAFE.values():
        assert rules(bound, time_units=units), bound


def test_a_unit_that_is_not_the_one_the_conversion_expects_is_flagged():
    units = {"interactions": "milliseconds", "market_prices": "milliseconds"}
    for name in ("product", "quotient", "to_timestamp", "epoch_ms", "make_timestamp"):
        assert rules(SAFE[name], time_units=units), name


def test_epoch_ms_of_a_timestamp_column_keeps_its_old_meaning():
    # on a real timestamp column epoch_ms(ts) is the number of milliseconds, as before
    spec = LeakageSpec.from_dict(
        {
            "label_table": "interactions",
            "entity_keys": ["user_id"],
            "prediction_ts": "ts",
            "label_columns": ["correct"],
            "time_columns": {"interactions": "ts", "events": "event_ts"},
        }
    )
    schema = {
        "interactions": {"user_id": "BIGINT", "ts": "TIMESTAMP", "correct": "BIGINT"},
        "events": {"user_id": "BIGINT", "event_ts": "TIMESTAMP", "v": "DOUBLE"},
    }
    sql = (
        "SELECT s.user_id, s.ts, s.correct, SUM(e.v) AS v FROM interactions s LEFT JOIN events e "
        "ON e.user_id = s.user_id AND epoch_ms(e.event_ts) < epoch_ms(s.ts) - 172800000 "
        "GROUP BY ALL"
    )
    assert {f.rule_id for f in static_findings(sql, spec, schema)} == set()


def test_a_derived_column_keeps_the_conversion_through_a_cte():
    sql = """WITH prices AS (SELECT user_id, to_timestamp(ts_s) AS at, price FROM market_prices),
spine AS (SELECT user_id, ts_ms, correct, to_timestamp(ts_ms / 1000) AS at FROM interactions)
SELECT s.user_id, s.ts_ms, s.correct, AVG(p.price) AS avg_price
FROM spine s LEFT JOIN prices p ON p.user_id = s.user_id AND p.at < s.at - INTERVAL 2 DAY
GROUP BY ALL"""
    spec = LeakageSpec.from_dict(SPEC)
    assert {f.rule_id for f in static_findings(sql, spec, SCHEMA)} == set()


# --- shapes found by an independent attack on the conversions ----------------------------------

NEGATIVE = [
    "p.ts_s * -1000 < s.ts_ms * -1",
    f"p.ts_s * -1000 < s.ts_ms * -1 + {TWO_DAYS_MS}",
    "p.ts_s / -1 < s.ts_ms / -1000",
    "s.ts_ms * -1 > p.ts_s * -1000",
    "(p.ts_s + 1) * -1000 < s.ts_ms * -1",
]


@pytest.mark.parametrize("bound", NEGATIVE)
def test_a_negative_factor_turns_the_comparison_round_so_it_bounds_nothing(bound):
    assert rules(bound), bound


def test_a_negative_factor_cannot_hide_the_labels_of_later_rows():
    spec = {
        "label_known_after": "0 seconds",
        "time_columns": {"interactions": "ts_ms"},
        "time_units": {"interactions": "milliseconds"},
    }
    sql = (
        "SELECT s.user_id, s.ts_ms, s.correct, AVG(h.correct) AS history FROM interactions s "
        "LEFT JOIN interactions h ON h.user_id = s.user_id AND h.ts_ms * -1 < s.ts_ms * -1 "
        "GROUP BY ALL"
    )
    assert {
        f.rule_id for f in static_findings(sql, LeakageSpec.from_dict({**SPEC, **spec}), SCHEMA)
    }


@pytest.mark.parametrize(
    "bound",
    [
        "p.ts_s * 1e-300 * 1e-300 < s.ts_ms * 1e-300 * 1e-300",
        "p.ts_s / 1e300 / 1e300 < s.ts_ms / 1e300 / 1e300",
        "p.ts_s * 1e300 * 1e300 < s.ts_ms * 1e300 * 1e300",
    ],
)
def test_a_factor_that_underflows_or_overflows_is_not_a_unit_and_does_not_crash(bound):
    assert "PARSE" not in rules(bound)
    assert rules(bound)


@pytest.mark.parametrize(
    "bound",
    [
        "to_timestamp(p.ts_s * 0.9999999991) < to_timestamp(s.ts_ms / 1000)",
        "p.ts_s * 999.9999999 < s.ts_ms",
        "p.ts_s < CAST(s.ts_ms / 1000 AS BIGINT)",
        "p.ts_s * 1000 < CAST(s.ts_ms / 1000 AS BIGINT) * 1000",
    ],
)
def test_a_factor_that_is_nearly_a_unit_or_a_rounding_cast_bounds_nothing(bound):
    assert rules(bound), bound


# --- second attack: the size of a truncation, and factors that are only nearly a unit ----------

EV = {
    "time_columns": {"interactions": "ts_ms", "ev": "ts_ms"},
    "time_units": {"interactions": "milliseconds", "ev": "milliseconds"},
}
EV_SCHEMA = {**SCHEMA, "ev": {"user_id": "BIGINT", "ts_ms": "BIGINT", "price": "DOUBLE"}}


def ev_rules(bound: str) -> set[str]:
    spec = LeakageSpec.from_dict({**SPEC, **EV})
    sql = (
        "SELECT s.user_id, s.ts_ms, s.correct, AVG(p.price) AS avg_price FROM interactions s "
        f"LEFT JOIN ev p ON p.user_id = s.user_id AND {bound} GROUP BY ALL"
    )
    return {f.rule_id for f in static_findings(sql, spec, EV_SCHEMA)}


@pytest.mark.parametrize(
    "bound",
    [
        # the event time is floored to the hour (or minute, second) and scaled back
        "p.ts_ms // 3600000 * 3600000 < s.ts_ms - 1000",
        "p.ts_ms // 1000 * 1000 < s.ts_ms - 1",
        "p.ts_ms // 60000 * 60000 < s.ts_ms - 1",
        "epoch_ms(p.ts_ms // 1000 * 1000) < epoch_ms(s.ts_ms) - INTERVAL 1 MILLISECOND",
        "to_timestamp(p.ts_ms // 60000 * 60) < to_timestamp(s.ts_ms / 1000) - INTERVAL 1 SECOND",
        # the prediction time is divided exactly (DuckDB `/`), the event time is floored
        "p.ts_ms // 1000 < s.ts_ms / 1000",
        "to_timestamp(p.ts_ms // 1000) < to_timestamp(s.ts_ms / 1000)",
        "p.ts_ms // 60000 < s.ts_ms // 1000 / 60",
    ],
)
def test_a_floor_is_sized_where_it_happens_and_an_exact_division_earns_no_credit(bound):
    assert ev_rules(bound), bound


def test_a_floor_that_leaves_room_for_it_stays_clean():
    # events floored to the second and required to end two seconds before the prediction
    assert ev_rules("p.ts_ms // 1000 * 1000 < s.ts_ms - 2000") == set()
    assert ev_rules("p.ts_ms // 1000 < s.ts_ms // 1000 - 2") == set()


@pytest.mark.parametrize(
    "bound",
    [
        "p.ts_s * 999.999999999 < s.ts_ms",
        "p.ts_s * 999.9999999999 < s.ts_ms",
        "to_timestamp(p.ts_s * 0.9999999999991) < to_timestamp(s.ts_ms / 1000)",
        "p.ts_s < s.ts_ms / 999.9999999996",
        "p.ts_s * 1000 < s.ts_ms * 1.0000000000004",
    ],
)
def test_a_factor_must_be_the_unit_exactly(bound):
    assert rules(bound), bound


# --- third attack: float rounding, chained floors, overflow ------------------------------------

SELF = (
    "SELECT s.user_id, s.ts_ms, s.correct, AVG(h.correct) AS past FROM interactions s "
    "LEFT JOIN interactions h ON h.user_id = s.user_id AND {c} GROUP BY ALL"
)
HISTORY = {
    "label_known_after": "0 seconds",
    "time_columns": {"interactions": "ts_ms"},
    "time_units": {"interactions": "milliseconds"},
}


def self_rules(condition: str) -> set[str]:
    spec = LeakageSpec.from_dict({**SPEC, **HISTORY})
    return {f.rule_id for f in static_findings(SELF.format(c=condition), spec, SCHEMA)}


@pytest.mark.parametrize(
    "condition",
    [
        # a factor that is not exact in a double: the two sides round differently at run time
        "h.ts_ms * 0.001 < CAST(s.ts_ms AS DOUBLE) * 0.001",
        "h.ts_ms * 0.001 < s.ts_ms * 1e-3",
        "h.ts_ms < s.ts_ms * 1e-3 * 1e3",
        "h.ts_ms * 0.1 < s.ts_ms * 1e-1",
        # two floors in a row drop a unit each
        "h.ts_ms // 1000 * 1000 // 1001 * 1001 < s.ts_ms - 1001",
        # a factor so large or small that both sides saturate
        "h.ts_ms * 1e300 <= (s.ts_ms - 1) * 1e300",
        "h.ts_ms / 1e-300 <= (s.ts_ms - 1) / 1e-300",
        "h.ts_ms * 1e-300 * 1e-300 <= (s.ts_ms - 1) * 1e-300 * 1e-300",
        "h.ts_ms * 1e-400 < s.ts_ms * 1e-400",
    ],
)
def test_a_conversion_that_only_looks_exact_bounds_nothing(condition):
    assert self_rules(condition), condition


def test_a_chain_of_floors_adds_up_their_steps():
    assert ev_rules("p.ts_ms // 1000 * 1000 // 1001 * 1001 < s.ts_ms - 1001")
    assert ev_rules("p.ts_ms // 1000 * 1000 // 1001 * 1001 < s.ts_ms - 2001") == set()


def test_a_literal_of_a_million_digits_does_not_stall_the_check():
    import time

    started = time.perf_counter()
    assert self_rules("h.ts_ms * 1e10000000 < s.ts_ms * 1e10000000")
    assert time.perf_counter() - started < 5
