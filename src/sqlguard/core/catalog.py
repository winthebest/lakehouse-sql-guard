"""Thin wrapper over a PyIceberg catalog: schemas for sqlglot, snapshots, refs, Arrow reads.

Only this module and the safety executor talk to PyIceberg directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pyarrow as pa
from pyiceberg.catalog import Catalog
from pyiceberg.catalog.sql import SqlCatalog
from pyiceberg.table import Table
from pyiceberg.types import (
    BinaryType,
    BooleanType,
    DateType,
    DecimalType,
    DoubleType,
    FloatType,
    IcebergType,
    IntegerType,
    LongType,
    StringType,
    TimestampType,
    TimestamptzType,
)

from sqlguard.core.lineage import Schema

DEFAULT_NAMESPACE = "db"

_SQL_TYPES: dict[type[IcebergType], str] = {
    BooleanType: "BOOLEAN",
    IntegerType: "INT",
    LongType: "BIGINT",
    FloatType: "FLOAT",
    DoubleType: "DOUBLE",
    StringType: "VARCHAR",
    BinaryType: "BLOB",
    DateType: "DATE",
    TimestampType: "TIMESTAMP",
    TimestamptzType: "TIMESTAMPTZ",
}


def _sql_type(iceberg_type: IcebergType) -> str:
    if isinstance(iceberg_type, DecimalType):
        return f"DECIMAL({iceberg_type.precision}, {iceberg_type.scale})"
    return _SQL_TYPES.get(type(iceberg_type), "VARCHAR")


@dataclass(frozen=True)
class SnapshotInfo:
    snapshot_id: int
    parent_id: int | None
    timestamp_ms: int
    operation: str


class LakehouseCatalog:
    """Access to the Iceberg tables of one namespace, addressed by bare table name."""

    def __init__(self, catalog: Catalog, namespace: str = DEFAULT_NAMESPACE) -> None:
        self.catalog = catalog
        self.namespace = namespace

    @classmethod
    def local(cls, warehouse: Path | str, namespace: str = DEFAULT_NAMESPACE) -> LakehouseCatalog:
        """Open (or create) a file-based catalog: SQLite metadata + local warehouse directory."""
        root = Path(warehouse).resolve()
        root.mkdir(parents=True, exist_ok=True)
        catalog = SqlCatalog(
            "sqlguard", uri=f"sqlite:///{root / 'catalog.db'}", warehouse=f"file://{root}"
        )
        catalog.create_namespace_if_not_exists(namespace)
        return cls(catalog, namespace)

    def identifier(self, table: str) -> str:
        return table if "." in table else f"{self.namespace}.{table}"

    def table(self, table: str) -> Table:
        return self.catalog.load_table(self.identifier(table))

    def table_names(self) -> list[str]:
        return sorted(name for _, name in self.catalog.list_tables(self.namespace))

    def has_table(self, table: str) -> bool:
        return self.catalog.table_exists(self.identifier(table))

    def schema_for(self, tables: list[str] | None = None) -> Schema:
        """sqlglot-compatible schema for `tables` (default: every table in the namespace)."""
        names = tables if tables is not None else self.table_names()
        return {
            name: {f.name: _sql_type(f.field_type) for f in self.table(name).schema().fields}
            for name in names
        }

    def create_table(self, table: str, data: pa.Table) -> Table:
        """Create a table from an Arrow schema and write `data` as its first snapshot."""
        iceberg_table = self.catalog.create_table(self.identifier(table), schema=data.schema)
        iceberg_table.append(data)
        return self.table(table)

    def snapshots(self, table: str) -> list[SnapshotInfo]:
        return [
            SnapshotInfo(
                snapshot_id=s.snapshot_id,
                parent_id=s.parent_snapshot_id,
                timestamp_ms=s.timestamp_ms,
                operation=str(s.summary.operation.value) if s.summary else "unknown",
            )
            for s in self.table(table).snapshots()
        ]

    def refs(self, table: str) -> dict[str, int]:
        return {name: ref.snapshot_id for name, ref in self.table(table).refs().items()}

    def read(self, table: str, snapshot_id: int | None = None) -> pa.Table:
        """Read a table (optionally at a snapshot) into Arrow."""
        return self.table(table).scan(snapshot_id=snapshot_id).to_arrow()
