"""L1 future reference and L2 join without a time inequality to prediction_ts."""

from __future__ import annotations

from sqlguard.core.findings import Finding, Severity
from sqlguard.leakage.analysis import QueryAnalysis, TableRef

DAY = 86_400


def future_bounds(ref: TableRef):
    """Bounds admitting facts at/after prediction time by a day or more."""
    lower = [b for b in ref.bounds if not b.is_upper and b.offset_seconds >= 0]
    far_upper = [b for b in ref.bounds if b.is_upper and b.offset_seconds >= DAY]
    return lower + far_upper


def check(analysis: QueryAnalysis) -> list[Finding]:
    findings = []
    for ref in analysis.temporal_refs:
        future = future_bounds(ref)
        if future:
            findings.append(
                Finding(
                    rule_id="L1",
                    severity=Severity.HIGH,
                    message=f"{ref.describe()} reads facts at or after prediction time",
                    location=ref.describe(),
                    evidence={"predicates": [b.sql for b in future]},
                )
            )
            continue
        if not any(b.is_upper for b in ref.bounds) and not ref.aggregated:
            time_col = analysis.spec.time_columns[ref.table]
            findings.append(
                Finding(
                    rule_id="L2",
                    severity=Severity.HIGH,
                    message=(
                        f"{ref.describe()} is joined without bounding {time_col} "
                        f"before {analysis.spec.prediction_ts}"
                    ),
                    location=ref.describe(),
                    evidence={"bounds": [b.sql for b in ref.bounds]},
                )
            )
    return findings
