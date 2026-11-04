"""Thin wrapper over a PyIceberg catalog: schemas for sqlglot, snapshots, refs, Arrow reads.

Only this module and the safety executor talk to PyIceberg directly. `LakehouseCatalog.local`
opens the file-based catalog used by tests and demos; `LakehouseCatalog.from_config` opens any
catalog PyIceberg can load (REST, Glue, SQL).
"""

from __future__ import annotations

import argparse
import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import pyarrow as pa
from pyiceberg.catalog import Catalog, load_catalog
from pyiceberg.catalog.sql import SqlCatalog
from pyiceberg.exceptions import NoSuchNamespaceError
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
from pyiceberg.utils.config import Config, merge_config

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


_SENSITIVE_KEY = re.compile(r"credential|token|secret|passw|pwd|key", re.IGNORECASE)
_URL_USERINFO = re.compile(r"(://)\S*@")
_KEY_VALUE = re.compile(
    r"""(?P<key>[\w.\-]*(?:credential|token|secret|passw|pwd|key)[\w.\-]*)(?P<sep>['"]?\s*[:=]\s*)"""
    r"""(?P<quote>['"]?)(?P<value>[^\s,'"}&]+)""",
    re.IGNORECASE,
)


def _secret_values(properties: Mapping[str, object]) -> set[str]:
    """Values in `properties` that must never be shown: sensitive keys and URL passwords."""
    found: set[str] = set()
    for key, value in properties.items():
        if not isinstance(value, str) or not value:
            continue
        if _SENSITIVE_KEY.search(str(key)):
            found.add(value)
        if "://" in value:
            try:
                password = urlsplit(value).password
            except ValueError:
                password = None
            if password:
                found.add(password)
    return found


def redact(text: str, secrets: Collection[str] = ()) -> str:
    """`text` without URL credentials (`scheme://user:pw@host`) or values of secret-looking keys.

    `secrets` are values known to be sensitive; they are removed wherever they appear.
    """
    for secret in sorted(secrets, key=len, reverse=True):
        text = text.replace(secret, "***")
    text = _URL_USERINFO.sub(r"\1***@", text)
    return _KEY_VALUE.sub(lambda m: f"{m['key']}{m['sep']}{m['quote']}***", text)


def add_catalog_args(parser: argparse.ArgumentParser) -> None:
    """Add the flags that select the lakehouse: `--catalog NAME` or `--warehouse PATH`.

    Shared by every CLI entry point so they open catalogs the same way; read the result with
    `catalog_from_args`. The namespace flag is `--catalog-namespace` because some commands
    already use `--namespace` for something else.
    """
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--catalog",
        metavar="NAME",
        help="PyIceberg catalog name (~/.pyiceberg.yaml or PYICEBERG_CATALOG__* variables)",
    )
    group.add_argument("--warehouse", help="local Iceberg warehouse (schema + snapshots)")
    parser.add_argument(
        "--catalog-namespace",
        default=DEFAULT_NAMESPACE,
        metavar="NS",
        help=f"namespace of bare table names (default: {DEFAULT_NAMESPACE})",
    )


def catalog_from_args(args: argparse.Namespace) -> LakehouseCatalog | None:
    """The catalog chosen by `add_catalog_args` flags, or None when neither was given."""
    namespace = getattr(args, "catalog_namespace", DEFAULT_NAMESPACE)
    if getattr(args, "catalog", None):
        return LakehouseCatalog.from_config(args.catalog, namespace=namespace)
    if getattr(args, "warehouse", None):
        return LakehouseCatalog.local(args.warehouse, namespace)
    return None


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

    @classmethod
    def from_config(
        cls,
        name: str | None = None,
        properties: Mapping[str, str] | None = None,
        namespace: str = DEFAULT_NAMESPACE,
    ) -> LakehouseCatalog:
        """Open any PyIceberg catalog (REST, Glue, SQL, ...) configured the PyIceberg way.

        `name` selects the entry under `catalog:` in `~/.pyiceberg.yaml` (or `$PYICEBERG_HOME`),
        or the `PYICEBERG_CATALOG__<NAME>__*` environment variables; None means the configured
        default catalog. `properties` override those values. The configuration is read when this
        is called, not when PyIceberg was imported. Unlike `local`, nothing is created: the
        namespace must already exist. Raises ValueError for a catalog that cannot be opened.

        A SQL catalog keeps each table under the catalog *name* it was created with, so a
        warehouse made by `local` (named `sqlguard`) is read back with `name="sqlguard"`.
        """
        secrets: set[str] = set()
        try:
            config = Config()
            name = name or config.get_default_catalog_name()
            merged = merge_config(config.get_catalog_config(name) or {}, dict(properties or {}))
            secrets = _secret_values(merged)
            catalog = load_catalog(name, **merged)
            found = catalog.namespace_exists(namespace)
        except NoSuchNamespaceError:
            found = False
        except Exception as err:  # noqa: BLE001 - bad config, unreachable service, bad credentials
            # PyIceberg's messages can quote the URI or properties, credentials included; `from
            # None` also keeps them out of tracebacks.
            raise ValueError(f"cannot open catalog {name!r}: {redact(str(err), secrets)}") from None
        if not found:
            raise ValueError(f"namespace {namespace!r} does not exist in catalog {name!r}")
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
        The logical-ts property means "when this commit became readable"; the visibility time of
        a snapshot is the latest of its own and its ancestors'.
        With `strict_history`, raise HistoryGap when the state at `ts` predates retained
        history (snapshots were expired), instead of silently answering "empty".
        """
        ts = naive_utc(ts)
        chain = self._main_chain(table)
        # A snapshot holds everything committed before it, so it is only visible once its
        # ancestors are: a late backfill tagged with an early logical time does not make the
        # data committed before it visible earlier.
        visible: SnapshotInfo | None = None
        seen_at = None
        for snapshot in chain:
            seen_at = snapshot.visible_at if seen_at is None else max(seen_at, snapshot.visible_at)
            if seen_at > ts:
                break
            visible = snapshot
        if visible is not None:
            return visible.snapshot_id
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
