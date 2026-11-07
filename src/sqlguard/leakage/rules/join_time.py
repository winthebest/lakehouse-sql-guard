"""L1 future reference and L2 join without a time inequality to prediction_ts."""

from __future__ import annotations

from sqlguard.core.findings import Finding, Severity
from sqlguard.leakage.analysis import QueryAnalysis, TimeBound
from sqlguard.leakage.roles import attribute

DAY = 86_400


def future_bounds(group: list[TimeBound]) -> list[TimeBound]:
    """Bounds admitting facts at/after prediction time, or up to a day or more after it."""
    lower = [b for b in group if not b.is_upper and b.offset_seconds >= 0]
    far_upper = [b for b in group if b.is_upper and b.offset_seconds >= DAY]
    return lower + far_upper


def check(analysis: QueryAnalysis) -> list[Finding]:
    spec = analysis.spec
    findings = []
    for ref in analysis.temporal_refs:
        for group in ref.bound_groups():
            future = future_bounds(group)
            if future:
                findings.append(
                    Finding(
                        "L1",
                        Severity.HIGH,
                        f"{ref.describe()} reads facts at or after prediction time",
                        ref.describe(),
                        {"predicates": [b.sql for b in future], "table": ref.table},
                    )
                )
                break
            if not any(b.is_upper for b in group) and not ref.aggregated:
                findings.append(
                    Finding(
                        "L2",
                        Severity.HIGH,
                        f"{ref.describe()} is joined without bounding "
                        f"{ref.time_column} before {spec.prediction_ts}",
                        ref.describe(),
                        {"bounds": [b.sql for b in group], "table": ref.table},
                    )
                )
                break
    for issue in analysis.windows:
        if issue.kind == "lookahead":
            finding = Finding(
                "L1",
                Severity.HIGH,
                f"window over {issue.ref.describe()} looks at later rows",
                issue.ref.describe(),
                {"window": issue.sql, "table": issue.ref.table},
            )
            findings.append(attribute(analysis, finding, issue.node))
    return findings
