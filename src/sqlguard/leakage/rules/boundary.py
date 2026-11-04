"""L5 boundary and timezone errors in the upper time bound."""

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
        if future_bounds(ref):
            continue  # already a future reference (L1)
        upper = [b for b in ref.bounds if b.is_upper]
        if upper and not any(b.is_strict_past for b in upper):
            inclusive = [b for b in upper if b.op == "<=" and b.offset_seconds == 0]
            shifted = [b for b in upper if 0 < b.offset_seconds < DAY]
            if inclusive:
                findings.append(
                    Finding(
                        "L5",
                        Severity.HIGH,
                        f"{ref.describe()} uses an inclusive bound: facts stamped exactly at "
                        f"{spec.prediction_ts} were not yet available",
                        ref.describe(),
                        {"predicates": [b.sql for b in inclusive]},
                    )
                )
            if shifted:
                findings.append(
                    Finding(
                        "L5",
                        Severity.HIGH,
                        f"{ref.describe()} is bounded at {spec.prediction_ts} shifted forward "
                        "(timezone or offset error)",
                        ref.describe(),
                        {"predicates": [b.sql for b in shifted]},
                    )
                )
        for bound in ref.bounds:
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
