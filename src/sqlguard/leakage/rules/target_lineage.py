"""L6 features derived from the label or from data observed after the outcome.

Three checks: column lineage of every output feature; any reference to an outcome table;
and any use of a label column other than passing it through or filtering the final rows.
"""

from __future__ import annotations

from sqlguard.core.findings import Finding, Severity
from sqlguard.core.lineage import LineageError, SourceColumn, full_lineage
from sqlguard.leakage.analysis import QueryAnalysis


def _is_passthrough(column: str, sources: set[SourceColumn], analysis: QueryAnalysis) -> bool:
    spec = analysis.spec
    if column in (spec.prediction_ts, *spec.label_columns):
        return sources == {SourceColumn(spec.label_table, column)}
    if column in spec.entity_keys:
        return bool(sources) and all(s.column == column for s in sources)
    return False


def check(analysis: QueryAnalysis) -> list[Finding]:
    spec = analysis.spec
    findings = []
    try:
        lineage = full_lineage(analysis.qualified, analysis.schema, spec.dialect)
    except LineageError as err:
        findings.append(Finding("LINEAGE", Severity.MEDIUM, f"lineage unavailable: {err}"))
        lineage = {}
    for column, sources in lineage.items():
        sources = {SourceColumn(s.table.lower(), s.column.lower()) for s in sources}
        if _is_passthrough(column.lower(), sources, analysis):
            continue
        hits = sorted(
            s for s in sources if s in spec.target_sources or s.table in spec.outcome_tables
        )
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
    seen = {f.location for f in findings}
    for ref in analysis.outcome_refs:
        if ref.describe() not in seen:
            seen.add(ref.describe())
            findings.append(
                Finding(
                    "L6",
                    Severity.HIGH,
                    f"query reads {ref.table}, which holds facts observed after the outcome",
                    ref.describe(),
                )
            )
    for use in analysis.label_uses:
        findings.append(
            Finding(
                "L6",
                Severity.HIGH,
                f"label column {use.column!r} is used to compute or select feature rows",
                use.ref.describe(),
                {"expression": use.sql},
            )
        )
    return findings
