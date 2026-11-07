"""A wall-clock cutoff on the spine's own time, written in the spine's unit.

`WHERE s.ts_ms < epoch_ms(now() - INTERVAL 7 DAY)` picks which predictions to train on exactly as
`to_timestamp(s.ts_ms / 1000) < now() - INTERVAL 7 DAY` does; it is the same cutoff in another
unit. A clock anywhere that a feature can see it is still flagged.
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


def rules(sql: str) -> set[str]:
    return {f.rule_id for f in static_findings(sql, LeakageSpec.from_dict(SPEC), SCHEMA)}


FILTER = "SELECT s.user_id, s.ts_ms, s.correct FROM interactions s WHERE {cond}"
SAME_CUTOFF = [
    "to_timestamp(s.ts_ms / 1000) < now() - INTERVAL 7 DAY",
    "s.ts_ms < epoch_ms(now() - INTERVAL 7 DAY)",
    "s.ts_ms < epoch_ms(now()) - 604800000",
    "s.ts_ms < CAST(epoch(now()) AS BIGINT) * 1000 - 604800000",
    "s.ts_ms / 1000 < epoch(now() - INTERVAL 7 DAY)",
    "s.ts_ms // 1000 < CAST(epoch(now() - INTERVAL 7 DAY) AS BIGINT)",
    "s.ts_ms < epoch_ms(current_timestamp - INTERVAL 7 DAY)",
]


@pytest.mark.parametrize("cond", SAME_CUTOFF)
def test_a_wall_clock_cutoff_on_the_spine_is_clean_in_any_unit(cond):
    assert rules(FILTER.format(cond=cond)) == set()


@pytest.mark.parametrize(
    "sql",
    [
        # the clock is a value a feature can see
        "SELECT s.user_id, s.ts_ms, s.correct, epoch_ms(now()) - s.ts_ms AS age_ms "
        "FROM interactions s",
        "SELECT s.user_id, s.ts_ms, s.correct, epoch_ms(now() - INTERVAL 7 DAY) AS cut "
        "FROM interactions s",
        "SELECT s.user_id, s.ts_ms, s.correct, CAST(epoch(now()) AS BIGINT) * 1000 AS cut "
        "FROM interactions s",
        # the clock bounds a feature table, not the spine
        "SELECT s.user_id, s.ts_ms, s.correct, AVG(p.price) AS avg_price FROM interactions s "
        "LEFT JOIN market_prices p ON p.user_id = s.user_id "
        "AND p.ts_s * 1000 < epoch_ms(now() - INTERVAL 7 DAY) GROUP BY ALL",
        # the clock is compared with a column that is not the spine's time
        "SELECT s.user_id, s.ts_ms, s.correct FROM interactions s "
        "WHERE s.x < epoch_ms(now() - INTERVAL 7 DAY)",
        "SELECT s.user_id, s.ts_ms, s.correct FROM interactions s "
        "WHERE s.ts_ms < epoch_ms(now() - INTERVAL 7 DAY) + s.x",
        # the clock decides which feature value a joined row gets
        "SELECT s.user_id, s.ts_ms, s.correct, p.price FROM interactions s "
        "LEFT JOIN market_prices p ON p.user_id = s.user_id "
        "AND s.ts_ms < epoch_ms(now() - INTERVAL 7 DAY)",
    ],
)
def test_a_clock_a_feature_can_see_is_still_flagged(sql):
    assert "L7" in rules(sql), sql
