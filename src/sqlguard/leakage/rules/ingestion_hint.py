"""L4 (static hint): bounded by event time while the table declares an ingestion time.

Event-time bounds look correct, but rows for the past can still arrive after prediction
time. Only replay over snapshot history can confirm it, so this rule is weighted below the
leaky threshold on its own: it raises suspicion, replay decides.
"""

from __future__ import annotations

from sqlguard.core.findings import Finding, Severity
from sqlguard.leakage.analysis import QueryAnalysis


def check(analysis: QueryAnalysis) -> list[Finding]:
    spec = analysis.spec
    findings = []
    for ref in analysis.temporal_refs:
        availability = spec.availability_columns.get(ref.table)
        if not availability:
            continue
        for group in ref.bound_groups():
            upper = [b for b in group if b.is_upper]
            if not upper or any(b.column_role == "availability" for b in upper):
                continue
            findings.append(
                Finding(
                    "L4",
                    Severity.MEDIUM,
                    f"{ref.describe()} is bounded by event time only; rows ingested after "
                    f"{spec.prediction_ts} ({availability}) may leak in",
                    ref.describe(),
                    {"predicates": [b.sql for b in upper], "availability_column": availability},
                )
            )
            break
    return findings
