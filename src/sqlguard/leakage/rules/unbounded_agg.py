"""L3 aggregate or window over a temporal table without an upper bound at prediction_ts."""

from __future__ import annotations

from sqlguard.core.findings import Finding, Severity
from sqlguard.leakage.analysis import QueryAnalysis
from sqlguard.leakage.rules.join_time import future_bounds


def check(analysis: QueryAnalysis) -> list[Finding]:
    findings = []
    for ref in analysis.temporal_refs:
        if not ref.aggregated:
            continue
        for group in ref.bound_groups():
            if future_bounds(group) or any(b.is_upper for b in group):
                continue
            findings.append(
                Finding(
                    "L3",
                    Severity.HIGH,
                    f"aggregate over {ref.describe()} has no upper bound at "
                    f"{analysis.spec.prediction_ts}; it sums the whole history",
                    ref.describe(),
                    {"bounds": [b.sql for b in group]},
                )
            )
            break
    for issue in analysis.windows:
        if issue.kind == "whole_partition":
            findings.append(
                Finding(
                    "L3",
                    Severity.HIGH,
                    f"window aggregate over {issue.ref.describe()} spans the whole partition, "
                    "including rows after prediction time",
                    issue.ref.describe(),
                    {"window": issue.sql},
                )
            )
    return findings
