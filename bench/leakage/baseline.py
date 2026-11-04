"""Baseline from the course brief: flag a training set only when an output row carries a
feature timestamp later than its prediction timestamp.

Score = fraction of output rows with some timestamp column > prediction_ts. Queries that do
not output a feature timestamp can never be flagged, which is exactly the gap the guard
targets.
"""

from __future__ import annotations

from datetime import datetime

import duckdb

from sqlguard.leakage.spec import LeakageSpec


def baseline_score(conn: duckdb.DuckDBPyConnection, sql: str, spec: LeakageSpec) -> float:
    try:
        result = conn.sql(sql)
        names = result.columns
        rows = result.fetchall()
    except duckdb.Error:
        return 0.0
    if spec.prediction_ts not in names or not rows:
        return 0.0
    ts_index = names.index(spec.prediction_ts)
    others = [i for i, name in enumerate(names) if i != ts_index]
    violations = 0
    for row in rows:
        prediction = row[ts_index]
        if any(
            isinstance(row[i], datetime) and prediction is not None and row[i] > prediction
            for i in others
        ):
            violations += 1
    return violations / len(rows)
