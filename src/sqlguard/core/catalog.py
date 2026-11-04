"""Thin wrapper over a PyIceberg catalog: schemas for sqlglot, snapshots, refs, Arrow reads.

Only this module and the safety executor talk to PyIceberg directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
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

# Optional snapshot-summary property holding the *logical* time a snapshot became visible
# (ISO-8601). Simulated histories set it; real tables fall back to the commit timestamp.
LOGICAL_TS_PROPERTY = "sqlguard.logical-ts"

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
    visible_at: datetime  # logical time if recorded, else commit time (naive UTC)


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
        iceberg_table = self.create_empty(table, data.schema)
        iceberg_table.append(data)
        return self.table(table)

    def create_empty(self, table: str, schema: pa.Schema) -> Table:
        return self.catalog.create_table(self.identifier(table), schema=schema)

    def snapshots(self, table: str) -> list[SnapshotInfo]:
        """Snapshots in commit order."""
        infos = []
        for s in self.table(table).snapshots():
            committed = datetime.fromtimestamp(s.timestamp_ms / 1000, tz=UTC).replace(tzinfo=None)
            logical = (
                s.summary.additional_properties.get(LOGICAL_TS_PROPERTY) if s.summary else None
            )
            infos.append(
                SnapshotInfo(
                    snapshot_id=s.snapshot_id,
                    parent_id=s.parent_snapshot_id,
                    timestamp_ms=s.timestamp_ms,
                    operation=str(s.summary.operation.value) if s.summary else "unknown",
                    visible_at=datetime.fromisoformat(logical) if logical else committed,
                )
            )
        return infos

    def snapshot_as_of(self, table: str, ts: datetime) -> int | None:
        """Latest snapshot visible at `ts` (None if the table did not exist yet).

        Follows the main-branch lineage so rolled-back or branch-only snapshots are ignored.
        """
        main_lineage = self._main_lineage(table)
        candidates = [s for s in self.snapshots(table) if s.snapshot_id in main_lineage]
        visible = [s for s in candidates if s.visible_at <= ts]
        return visible[-1].snapshot_id if visible else None

    def _main_lineage(self, table: str) -> set[int]:
        iceberg_table = self.table(table)
        current = iceberg_table.current_snapshot()
        lineage: set[int] = set()
        while current is not None:
            lineage.add(current.snapshot_id)
            parent = current.parent_snapshot_id
            current = iceberg_table.snapshot_by_id(parent) if parent is not None else None
        return lineage

    def history_truncated(self, table: str) -> bool:
        """True when older snapshots were expired, so early states cannot be time-travelled.

        Uses sequence numbers (a table's first snapshot is 1 in format v2) because expiry may
        also clear the surviving snapshot's parent pointer.
        """
        snapshots = self.table(table).snapshots()
        if not snapshots:
            return False
        oldest = min(snapshots, key=lambda s: s.sequence_number or 0)
        if oldest.sequence_number:
            return oldest.sequence_number > 1
        parent = oldest.parent_snapshot_id
        return parent is not None and self.table(table).snapshot_by_id(parent) is None

    def refs(self, table: str) -> dict[str, int]:
        return {name: ref.snapshot_id for name, ref in self.table(table).refs().items()}

    def read(self, table: str, snapshot_id: int | None = None) -> pa.Table:
        """Read a table (optionally at a snapshot) into Arrow."""
        return self.table(table).scan(snapshot_id=snapshot_id).to_arrow()
