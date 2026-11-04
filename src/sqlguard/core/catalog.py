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


def naive_utc(ts: datetime) -> datetime:
    """`ts` as a naive UTC datetime (naive input is taken to be UTC already)."""
    return ts.astimezone(UTC).replace(tzinfo=None) if ts.tzinfo else ts


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
    sequence_number: int | None = None


class HistoryGap(LookupError):
    """The table state at a requested time is older than its retained snapshot history."""


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
                    visible_at=naive_utc(datetime.fromisoformat(logical)) if logical else committed,
                    sequence_number=s.sequence_number,
                )
            )
        return infos

    def snapshot_as_of(
        self, table: str, ts: datetime, *, strict_history: bool = False
    ) -> int | None:
        """Latest main-branch snapshot visible at `ts` (None if the table did not exist yet).

        Follows the main-branch lineage, so rolled-back or branch-only snapshots are ignored.
        With `strict_history`, raise HistoryGap when the state at `ts` predates retained
        history (snapshots were expired), instead of silently answering "empty".
        """
        ts = naive_utc(ts)
        chain = self._main_chain(table)
        visible = [s for s in chain if s.visible_at <= ts]
        if visible:
            return visible[-1].snapshot_id
        if strict_history and chain and self._chain_truncated(table, chain):
            raise HistoryGap(
                f"history of {table!r} before {chain[0].visible_at.isoformat()} was expired; "
                f"the state at {ts.isoformat()} can no longer be reconstructed"
            )
        return None

    def _main_chain(self, table: str) -> list[SnapshotInfo]:
        """Main-branch snapshots reachable through parent pointers, oldest first."""
        infos = {s.snapshot_id: s for s in self.snapshots(table)}
        iceberg_table = self.table(table)
        current = iceberg_table.current_snapshot()
        chain: list[SnapshotInfo] = []
        while current is not None and current.snapshot_id in infos:
            chain.append(infos[current.snapshot_id])
            parent = current.parent_snapshot_id
            current = iceberg_table.snapshot_by_id(parent) if parent is not None else None
        return chain[::-1]

    def _chain_truncated(self, table: str, chain: list[SnapshotInfo]) -> bool:
        oldest = chain[0]
        if oldest.sequence_number:
            # Format v2: the first commit of a table has sequence number 1.
            return oldest.sequence_number > 1
        parent = oldest.parent_id
        return parent is not None and self.table(table).snapshot_by_id(parent) is None

    def history_truncated(self, table: str) -> bool:
        """True when older main-branch snapshots were expired (time travel is incomplete).

        Checks the main lineage, not all snapshots, so a tag that keeps one old snapshot
        alive does not hide expired history. Expiry may also clear the survivor's parent
        pointer, hence sequence numbers.
        """
        chain = self._main_chain(table)
        return bool(chain) and self._chain_truncated(table, chain)

    def refs(self, table: str) -> dict[str, int]:
        return {name: ref.snapshot_id for name, ref in self.table(table).refs().items()}

    def read(self, table: str, snapshot_id: int | None = None) -> pa.Table:
        """Read a table (optionally at a snapshot) into Arrow."""
        return self.table(table).scan(snapshot_id=snapshot_id).to_arrow()
