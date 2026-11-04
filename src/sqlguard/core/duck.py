"""Run SQL over Arrow tables with an in-memory DuckDB connection.

Iceberg tables are read with PyIceberg (optionally at a snapshot) and registered by bare
name, so no DuckDB extension or network access is needed.
"""

from __future__ import annotations

import duckdb
import pyarrow as pa

from sqlguard.core.catalog import LakehouseCatalog


def connect(tables: dict[str, pa.Table]) -> duckdb.DuckDBPyConnection:
    """New in-memory connection with each Arrow table materialized under its name."""
    conn = duckdb.connect()
    conn.execute("SET TimeZone = 'UTC'")
    for name, data in tables.items():
        conn.register(f"__arrow_{name}", data)
        conn.execute(f'CREATE TABLE "{name}" AS SELECT * FROM "__arrow_{name}"')
        conn.unregister(f"__arrow_{name}")
    return conn


def connect_catalog(
    catalog: LakehouseCatalog,
    tables: list[str] | None = None,
    snapshot_ids: dict[str, int | None] | None = None,
) -> duckdb.DuckDBPyConnection:
    """Load catalog tables (each optionally pinned to a snapshot) into a fresh connection."""
    names = tables if tables is not None else catalog.table_names()
    pins = snapshot_ids or {}
    return connect({name: catalog.read(name, pins.get(name)) for name in names})
