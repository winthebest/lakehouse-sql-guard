"""Run SQL over Arrow tables with an in-memory DuckDB connection.

Iceberg tables are read with PyIceberg (optionally at a snapshot) and registered by bare
name, so no DuckDB extension or network access is needed.
"""

from __future__ import annotations

import duckdb
import pyarrow as pa

from sqlguard.core.catalog import LakehouseCatalog


def connect(tables: dict[str, pa.Table], sandbox: bool = True) -> duckdb.DuckDBPyConnection:
    """New in-memory connection exposing each Arrow table under its name (zero-copy).

    With `sandbox` (the default) the connection cannot read or write files or reach the
    network, and the setting is locked, so untrusted SQL only sees the given tables.
    """
    conn = duckdb.connect()
    conn.execute("SET TimeZone = 'UTC'")
    for name, data in tables.items():
        conn.register(name, data)
    if sandbox:
        conn.execute("SET enable_external_access = false")
        conn.execute("SET lock_configuration = true")
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
