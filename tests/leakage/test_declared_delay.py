"""An event-time bound with less margin than the spec's declared max_ingest_delay is a finding of
full weight (L4), not a hint for replay to settle; without the declaration it stays a hint."""

import pytest

from sqlguard.leakage.spec import LeakageSpec
from sqlguard.leakage.validator import validate

from .conftest import SCHEMA, SPEC

SEL = "SELECT l.user_id, l.label_ts, l.churned"

EVENT_BUFFER = (
    f"{SEL}, SUM(e.amount) a FROM labels l LEFT JOIN events e ON e.user_id = l.user_id "
    "AND e.event_ts < l.label_ts - INTERVAL {buffer} "
    "AND e.event_ts >= l.label_ts - INTERVAL 90 DAY GROUP BY 1, 2, 3"
)
DELAY = {"max_ingest_delay": {"events": "8 days"}}


def scored(sql: str, **spec_overrides):
    report = validate(sql, LeakageSpec.from_dict({**SPEC, **spec_overrides}), schema=SCHEMA)
    return report.verdict, report.score, report.findings


@pytest.mark.parametrize("buffer", ["7 DAY", "1 DAY", "0 DAY", "191 HOUR", "'1 week'"])
def test_a_buffer_shorter_than_the_declared_delay_is_a_finding_not_a_hint(buffer):
    verdict, score, findings = scored(EVENT_BUFFER.format(buffer=buffer), **DELAY)
    assert verdict == "leaky" and score >= 0.5
    [finding] = findings
    assert finding.rule_id == "L4" and finding.evidence["exceeds_declared_delay"] is True
    assert "max_ingest_delay (8 days)" in finding.message


@pytest.mark.parametrize("buffer", ["8 DAY", "9 DAY", "192 HOUR", "'2 weeks'"])
def test_a_buffer_at_least_the_declared_delay_stays_clean(buffer):
    assert scored(EVENT_BUFFER.format(buffer=buffer), **DELAY)[0] == "clean"


@pytest.mark.parametrize("buffer", ["7 DAY", "0 DAY"])
def test_without_a_declared_delay_the_event_time_bound_stays_a_hint(buffer):
    verdict, score, findings = scored(EVENT_BUFFER.format(buffer=buffer))
    assert verdict == "unverifiable" and score < 0.5
    assert [f.rule_id for f in findings] == ["L4"] and "exceeds_declared_delay" not in findings[
        0
    ].evidence


def test_an_availability_bound_makes_the_declared_delay_irrelevant():
    sql = EVENT_BUFFER.format(buffer="1 DAY").replace("e.event_ts <", "e.ingested_at <", 1)
    assert scored(sql, **DELAY)[0] == "clean"
