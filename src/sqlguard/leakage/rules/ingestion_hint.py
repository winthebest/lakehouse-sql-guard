"""L4: bounded by event time while the table declares an ingestion time.

Event-time bounds look correct, but rows for the past can still arrive after prediction
time. Only replay over snapshot history can confirm it, so without a declared delay this rule is
weighted below the leaky threshold on its own: it raises suspicion, replay decides.

A table with a declared `max_ingest_delay` has a known worst case: an event-time bound at least
that far before prediction_ts admits only rows that had already landed, so no finding is raised
(the availability column, when the query uses it, is still the better bound). A bound with a
shorter margin admits rows the declaration says can still be on their way: that is a finding
of full weight (`exceeds_declared_delay`), not a hint, because the spec itself says the leak
window is open. Replay finding no row in that window at the sampled times does not close it.
"""

from __future__ import annotations

from sqlguard.core.findings import Finding, Severity
from sqlguard.leakage.analysis import QueryAnalysis


def _describe(seconds: float | None) -> str:
    if seconds is None:
        return ""
    for unit, size in (("day", 86_400), ("hour", 3600), ("minute", 60)):
        if seconds % size == 0:
            count = int(seconds // size)
            return f"{count} {unit}{'' if count == 1 else 's'}"
    return f"{seconds:g} seconds"


def check(analysis: QueryAnalysis) -> list[Finding]:
    spec = analysis.spec
    findings = []
    for ref in analysis.temporal_refs:
        availability = spec.availability_columns.get(ref.table)
        if not availability:
            continue
        delay = spec.max_ingest_delay.get(ref.table)
        for group in ref.bound_groups():
            upper = [b for b in group if b.is_upper]
            if not upper or any(b.column_role == "availability" for b in upper):
                continue
            if delay is not None and any(b.covers(delay) for b in upper):
                continue
            evidence = {"predicates": [b.sql for b in upper], "availability_column": availability}
            if delay is not None:
                evidence |= {"exceeds_declared_delay": True, "max_ingest_delay_seconds": delay}
            reason = (
                f"by less than the declared max_ingest_delay ({_describe(delay)})"
                if delay is not None
                else "only"
            )
            findings.append(
                Finding(
                    "L4",
                    Severity.MEDIUM if delay is None else Severity.HIGH,
                    f"{ref.describe()} is bounded by event time {reason}; rows ingested after "
                    f"{spec.prediction_ts} ({availability}) may leak in",
                    ref.describe(),
                    evidence,
                )
            )
            break
    return findings
