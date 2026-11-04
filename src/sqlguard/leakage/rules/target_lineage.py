"""L6 features whose column lineage reaches the label or an outcome-derived column."""

from __future__ import annotations

from sqlguard.core.findings import Finding, Severity
from sqlguard.core.lineage import LineageError, full_lineage
from sqlguard.leakage.analysis import QueryAnalysis


def check(analysis: QueryAnalysis) -> list[Finding]:
    spec = analysis.spec
    try:
        lineage = full_lineage(analysis.qualified, analysis.schema, spec.dialect)
    except LineageError as err:
        return [Finding("LINEAGE", Severity.INFO, f"lineage unavailable: {err}")]
    forbidden = spec.target_sources
    findings = []
    for column, sources in lineage.items():
        if column in spec.passthrough_columns:
            continue
        hits = sorted(s for s in sources if s in forbidden or s.table in spec.outcome_tables)
        if hits:
            findings.append(
                Finding(
                    "L6",
                    Severity.HIGH,
                    f"feature {column!r} is derived from the target or post-outcome data",
                    column,
                    {"sources": [f"{s.table}.{s.column}" for s in hits]},
                )
            )
    return findings
