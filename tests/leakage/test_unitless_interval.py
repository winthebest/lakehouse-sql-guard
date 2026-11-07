"""An INTERVAL written without a unit has no length the checker can rely on.

DuckDB and Postgres read `INTERVAL '10'` as ten seconds, other engines reject it. Either way it is
not "10 days", so a bound made with one is unknown: the table counts as unbounded, like every other
shape the checker cannot read. The same bound written with a unit stays a bound.
"""

import pytest

from sqlguard.leakage.spec import LeakageSpec
from sqlguard.leakage.validator import static_findings

from .conftest import SCHEMA, SPEC

STATS = (
    "SELECT l.user_id, l.label_ts, l.churned, SUM(s.total_spend) AS spend "
    "FROM labels l LEFT JOIN user_stats s ON s.user_id = l.user_id AND {cond} GROUP BY ALL"
)
EVENTS = (
    "SELECT l.user_id, l.label_ts, l.churned, SUM(e.amount) AS amount "
    "FROM labels l LEFT JOIN events e ON e.user_id = l.user_id AND {cond} GROUP BY ALL"
)
BOUNDS = {
    "lt": "s.updated_at < l.label_ts - INTERVAL {i}",
    "lte": "s.updated_at <= l.label_ts - INTERVAL {i}",
    "between": "s.updated_at BETWEEN l.label_ts - INTERVAL 60 DAY AND l.label_ts - INTERVAL {i}",
    "feature_side": "s.updated_at + INTERVAL {i} < l.label_ts",
    "reversed": "l.label_ts - INTERVAL {i} > s.updated_at",
}


def rules(sql: str, **overrides) -> set[str]:
    spec = LeakageSpec.from_dict({**SPEC, **overrides})
    return {f.rule_id for f in static_findings(sql, spec, SCHEMA)}


@pytest.mark.parametrize("shape", sorted(BOUNDS))
def test_a_bound_made_with_a_unitless_interval_is_no_bound(shape):
    cond = BOUNDS[shape]
    assert rules(STATS.format(cond=cond.format(i="3 DAY"))) == set()
    assert rules(STATS.format(cond=cond.format(i="'3'")))
    assert rules(STATS.format(cond=cond.format(i="3")))


@pytest.mark.parametrize("shape", sorted(BOUNDS))
def test_a_unitless_interval_does_not_cover_a_declared_ingest_delay(shape):
    cond = BOUNDS[shape].replace("s.updated_at", "e.event_ts")
    delay = {"max_ingest_delay": {"events": "8 days"}}
    assert rules(EVENTS.format(cond=cond.format(i="10 DAY")), **delay) == set()
    assert rules(EVENTS.format(cond=cond.format(i="'10'")), **delay)


def test_an_interval_with_the_unit_inside_the_string_keeps_its_length():
    assert rules(STATS.format(cond="s.updated_at < l.label_ts - INTERVAL '3 days'")) == set()
