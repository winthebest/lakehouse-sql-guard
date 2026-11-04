"""L7 non-deterministic time: `now()`, `current_date`, ... used where a feature can see it.

A feature computed from the wall clock (days since the last order as of *today*) changes with
every run and describes the moment the training set was built, not the prediction time the row
stands for. The same query gives a different training set tomorrow, and at serving time the
clock no longer matches. A filter that only chooses which spine rows to train on is exempt.
"""

from __future__ import annotations

from sqlguard.core.findings import Finding, Severity
from sqlguard.leakage.analysis import QueryAnalysis


def check(analysis: QueryAnalysis) -> list[Finding]:
    return [
        Finding(
            "L7",
            Severity.HIGH,
            f"{use.sql} depends on when the query runs, not on the prediction time of the row",
            use.sql,
        )
        for use in analysis.clock_uses
    ]
