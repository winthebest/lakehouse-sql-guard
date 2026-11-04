"""Property-based tests: rule verdicts hold across random aliases, windows, and offsets,
and as-of queries flagged clean never read facts at or after prediction time."""

from datetime import datetime, timedelta

import pyarrow as pa
from hypothesis import given, settings
from hypothesis import strategies as st

from sqlguard.core.duck import connect
from sqlguard.leakage.spec import LeakageSpec
from sqlguard.leakage.validator import static_findings
from tests.leakage.conftest import SCHEMA, SPEC

spec = LeakageSpec.from_dict(SPEC)

aliases = st.sampled_from(["e", "ev", "x1", "evt", "src"])
label_aliases = st.sampled_from(["l", "lab", "spine", "y"])
aggs = st.sampled_from(["SUM", "AVG", "MAX", "MIN", "COUNT"])
windows = st.integers(min_value=1, max_value=365)
hours = st.integers(min_value=1, max_value=23)


def _query(a, la, agg, window, bound):
    return (
        f"SELECT {la}.user_id, {la}.label_ts, {la}.churned, {agg}({a}.amount) AS f "
        f"FROM labels {la} LEFT JOIN events {a} ON {a}.user_id = {la}.user_id "
        f"AND {bound.format(a=a, la=la)} "
        f"AND {a}.event_ts >= {la}.label_ts - INTERVAL {window} DAY "
        f"GROUP BY {la}.user_id, {la}.label_ts, {la}.churned"
    )


def _rules(sql):
    return {f.rule_id for f in static_findings(sql, spec, SCHEMA)}


@settings(max_examples=200, deadline=None)
@given(a=aliases, la=label_aliases, agg=aggs, window=windows)
def test_strict_availability_bound_is_always_clean(a, la, agg, window):
    assert _rules(_query(a, la, agg, window, "{a}.ingested_at < {la}.label_ts")) == set()


@settings(max_examples=200, deadline=None)
@given(a=aliases, la=label_aliases, agg=aggs, window=windows)
def test_inclusive_bound_is_always_l5(a, la, agg, window):
    assert "L5" in _rules(_query(a, la, agg, window, "{a}.ingested_at <= {la}.label_ts"))


@settings(max_examples=200, deadline=None)
@given(a=aliases, la=label_aliases, agg=aggs, window=windows, h=hours)
def test_forward_shift_is_always_l5(a, la, agg, window, h):
    bound = "{a}.ingested_at < {la}.label_ts + INTERVAL " + str(h) + " HOUR"
    assert "L5" in _rules(_query(a, la, agg, window, bound))


@settings(max_examples=200, deadline=None)
@given(a=aliases, la=label_aliases, agg=aggs, window=windows)
def test_event_time_only_is_always_l4(a, la, agg, window):
    assert _rules(_query(a, la, agg, window, "{a}.event_ts < {la}.label_ts")) == {"L4"}


event_rows = st.lists(
    st.tuples(st.integers(1, 3), st.integers(0, 240), st.integers(0, 72)), min_size=1, max_size=40
)
label_rows = st.lists(st.tuples(st.integers(1, 3), st.integers(0, 300)), min_size=1, max_size=10)


@settings(max_examples=200, deadline=None)
@given(events=event_rows, labels=label_rows, window=st.integers(1, 10))
def test_clean_query_never_reads_unavailable_facts(events, labels, window):
    """Semantic check on DuckDB: as-of aggregation only sees rows ingested before label_ts."""
    t0 = datetime(2026, 1, 1)
    conn = connect(
        {
            "events": pa.table(
                {
                    "user_id": [u for u, _, _ in events],
                    "event_ts": [t0 + timedelta(hours=h) for _, h, _ in events],
                    "ingested_at": [t0 + timedelta(hours=h + d) for _, h, d in events],
                    "amount": [1.0] * len(events),
                }
            ),
            "labels": pa.table(
                {
                    "user_id": [u for u, _ in labels],
                    "label_ts": [t0 + timedelta(hours=h) for _, h in labels],
                    "churned": [0] * len(labels),
                }
            ),
        }
    )
    sql = (
        "SELECT l.label_ts, MAX(e.ingested_at) AS seen, MAX(e.event_ts) AS ev "
        "FROM labels l LEFT JOIN events e ON e.user_id = l.user_id "
        f"AND e.ingested_at < l.label_ts AND e.event_ts >= l.label_ts - INTERVAL {window} DAY "
        "GROUP BY l.user_id, l.label_ts"
    )
    for label_ts, seen, ev in conn.sql(sql).fetchall():
        assert seen is None or (seen < label_ts and ev < label_ts)
