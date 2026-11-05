"""Baseline from the course brief: flag a training set only when an output row carries a
feature timestamp later than its prediction timestamp.

Score = fraction of output rows with some timestamp column > prediction_ts. Queries that do
not output a feature timestamp can never be flagged, which is exactly the gap the guard
targets.
"""

from __future__ import annotations

from collections.abc import Mapping
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


def bind_relations(conn: duckdb.DuckDBPyConnection, spec: LeakageSpec) -> None:
    """Expose the spec's qualified relation names (`db.schema.model`) on `conn`.

    Each name becomes a view over the bare catalog table it maps to, so the baseline can run the
    queries that name tables the way a dbt project does. A spec without `relations` changes nothing.
    """
    bind_relation_map(conn, spec.relations)


def bind_relation_map(conn: duckdb.DuckDBPyConnection, relations: Mapping[str, str]) -> None:
    """`bind_relations` for a plain mapping of qualified name -> catalog table."""
    for name, target in relations.items():
        parts = [p.replace('"', '""') for p in name.split(".")]
        quoted = [f'"{p}"' for p in parts]
        if len(parts) == 3:
            attached = {
                row[0]
                for row in conn.execute("SELECT database_name FROM duckdb_databases()").fetchall()
            }
            if parts[0] not in attached:
                conn.execute(f"ATTACH ':memory:' AS {quoted[0]}")
        conn.execute(f"CREATE SCHEMA IF NOT EXISTS {'.'.join(quoted[:-1])}")
        source = target.rsplit(".", 1)[-1].replace('"', '""')
        view = ".".join(quoted)
        conn.execute(f'CREATE OR REPLACE VIEW {view} AS SELECT * FROM temp.main."{source}"')
