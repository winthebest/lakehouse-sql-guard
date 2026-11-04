"""L3 aggregate over a temporal table without an upper time bound at prediction_ts."""

from __future__ import annotations

from sqlguard.core.findings import Finding, Severity
from sqlguard.leakage.analysis import QueryAnalysis
from sqlguard.leakage.rules.join_time import future_bounds


def check(analysis: QueryAnalysis) -> list[Finding]:
    findings = []
    for ref in analysis.temporal_refs:
        if future_bounds(ref) or any(b.is_upper for b in ref.bounds) or not ref.aggregated:
            continue
        findings.append(
            Finding(
                rule_id="L3",
                severity=Severity.HIGH,
                message=(
                    f"aggregate over {ref.describe()} has no upper bound at "
                    f"{analysis.spec.prediction_ts}; it sums the whole history"
                ),
                location=ref.describe(),
                evidence={"bounds": [b.sql for b in ref.bounds]},
            )
        )
    return findings
