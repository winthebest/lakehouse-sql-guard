"""L5 boundary and timezone errors in an upper time bound.

Checked per time column: a strict bound on event time does not excuse an inclusive or
forward-shifted bound on ingestion time, and vice versa.
"""

from __future__ import annotations

from sqlguard.core.findings import Finding, Severity
from sqlguard.leakage.analysis import QueryAnalysis
from sqlguard.leakage.rules.join_time import DAY, future_bounds


def _tz_aware(sql_type: str) -> bool:
    return "TIMESTAMPTZ" in sql_type or "WITH TIME ZONE" in sql_type


def check(analysis: QueryAnalysis) -> list[Finding]:
    spec = analysis.spec
    label_type = analysis.column_type(spec.label_table, spec.prediction_ts)
    findings = []
    for ref in analysis.temporal_refs:
        for group in ref.bound_groups():
            if future_bounds(group):
                continue  # already a future reference (L1)
            loose = []
            for column in {b.column for b in group if b.is_upper}:
                upper = [b for b in group if b.is_upper and b.column == column]
                if any(b.is_strict_past for b in upper):
                    continue
                loose += [
                    b
                    for b in upper
                    if (b.op == "<=" and b.offset_seconds == 0) or 0 < b.offset_seconds < DAY
                ]
            if loose:
                findings.append(
                    Finding(
                        "L5",
                        Severity.HIGH,
                        f"{ref.describe()} admits facts stamped at or shortly after "
                        f"{spec.prediction_ts} (inclusive bound, offset, or timezone shift)",
                        ref.describe(),
                        {"predicates": sorted({b.sql for b in loose})},
                    )
                )
                break
        for bound in ref.bounds:
            if not bound.raw:
                continue  # an explicit cast on either side states the intended semantics
            feature_type = analysis.column_type(ref.table, bound.column)
            if feature_type and label_type and _tz_aware(feature_type) != _tz_aware(label_type):
                findings.append(
                    Finding(
                        "L5",
                        Severity.MEDIUM,
                        f"{ref.table}.{bound.column} ({feature_type}) is compared with "
                        f"{spec.prediction_ts} ({label_type}): mixed timezone semantics",
                        ref.describe(),
                        {"predicate": bound.sql},
                    )
                )
                break
    return findings
