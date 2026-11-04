"""Impact of a change: compare a table before and after, as rows and schema.

Rows are compared in DuckDB. With a usable unique key, rows are matched by key and split into
added / deleted / modified. Without one, whole rows are compared as multisets: a modified row
shows up as one deleted plus one added row, and `rows_modified` is None.

`rows_affected` is the number of pre-existing rows the change deleted or rewrote, which is what
the policy's blast-radius thresholds measure. Pure inserts therefore affect zero rows.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import duckdb
import pyarrow as pa

from sqlguard.safety.classifier import Impact


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


@dataclass(frozen=True)
class SchemaDiff:
    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    changed: tuple[str, ...] = ()  # "column: old_type -> new_type"

    @property
    def empty(self) -> bool:
        return not (self.added or self.removed or self.changed)

    def to_dict(self) -> dict[str, Any]:
        return {
            "added": list(self.added),
            "removed": list(self.removed),
            "changed": list(self.changed),
        }


@dataclass(frozen=True)
class TableImpact:
    table: str
    rows_before: int
    rows_after: int
    rows_added: int
    rows_deleted: int
    rows_modified: int | None  # None when no usable unique key was available
    key: tuple[str, ...] | None
    schema_diff: SchemaDiff = field(default_factory=SchemaDiff)

    @property
    def rows_affected(self) -> int:
        return self.rows_deleted + (self.rows_modified or 0)

    @property
    def affected_pct(self) -> float | None:
        return 100.0 * self.rows_affected / self.rows_before if self.rows_before else None

    @property
    def deleted_pct(self) -> float | None:
        return 100.0 * self.rows_deleted / self.rows_before if self.rows_before else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "table": self.table,
            "rows_before": self.rows_before,
            "rows_after": self.rows_after,
            "rows_added": self.rows_added,
            "rows_deleted": self.rows_deleted,
            "rows_modified": self.rows_modified,
            "rows_affected": self.rows_affected,
            "affected_pct": self.affected_pct,
            "key": list(self.key) if self.key else None,
            "schema_diff": self.schema_diff.to_dict(),
        }


@dataclass(frozen=True)
class ImpactReport:
    """Measured effect of an action on every table it changed."""

    tables: tuple[TableImpact, ...] = ()

    @property
    def schema_changed(self) -> bool:
        return any(not t.schema_diff.empty for t in self.tables)

    def to_impact(self) -> Impact:
        """Collapse to the policy's `Impact` (summed over tables)."""
        if not self.tables:
            return Impact(rows_affected=0, total_rows=0)
        return Impact(
            rows_affected=sum(t.rows_affected for t in self.tables),
            total_rows=sum(t.rows_before for t in self.tables),
        )

    def to_dict(self) -> dict[str, Any]:
        return {"tables": [t.to_dict() for t in self.tables], "schema_changed": self.schema_changed}


def schema_diff(before: pa.Schema, after: pa.Schema) -> SchemaDiff:
    old = {f.name: _type_name(f.type) for f in before}
    new = {f.name: _type_name(f.type) for f in after}
    return SchemaDiff(
        added=tuple(n for n in new if n not in old),
        removed=tuple(n for n in old if n not in new),
        changed=tuple(f"{n}: {old[n]} -> {new[n]}" for n in old if n in new and old[n] != new[n]),
    )


def _type_name(arrow_type: pa.DataType) -> str:
    # large_string vs string is an Arrow storage detail, not a schema change
    return str(arrow_type).replace("large_", "")


def compute_table_impact(
    table: str,
    before: pa.Table,
    after: pa.Table,
    key: Sequence[str] = (),
) -> TableImpact:
    """Diff `before` against `after`; `key` enables key-matched modified-row counting."""
    diff = schema_diff(before.schema, after.schema)
    common = [n for n in before.column_names if n in after.column_names]
    conn = duckdb.connect()
    try:
        conn.register("before_t", before)
        conn.register("after_t", after)
        usable_key = tuple(key) if key and _key_usable(conn, key, common) else None
        if usable_key is not None:
            added, deleted, modified = _keyed_counts(conn, usable_key, common)
        else:
            added, deleted, modified = _multiset_counts(conn, common, before, after)
    finally:
        conn.close()
    return TableImpact(
        table=table,
        rows_before=before.num_rows,
        rows_after=after.num_rows,
        rows_added=added,
        rows_deleted=deleted,
        rows_modified=modified,
        key=usable_key,
        schema_diff=diff,
    )


def _scalar(conn: duckdb.DuckDBPyConnection, sql: str) -> int:
    row = conn.execute(sql).fetchone()
    return int(row[0]) if row else 0


def _key_usable(conn: duckdb.DuckDBPyConnection, key: Sequence[str], common: list[str]) -> bool:
    """A key is usable when it exists on both sides and is non-null and unique on both."""
    if any(k not in common for k in key):
        return False
    cols = ", ".join(quote_ident(k) for k in key)
    has_null = " OR ".join(f"{quote_ident(k)} IS NULL" for k in key)
    for name in ("before_t", "after_t"):
        if _scalar(conn, f"SELECT count(*) FROM {name} WHERE {has_null}"):
            return False
        total = _scalar(conn, f"SELECT count(*) FROM {name}")
        distinct = _scalar(
            conn, f"SELECT count(*) FROM (SELECT {cols} FROM {name} GROUP BY {cols})"
        )
        if total != distinct:
            return False
    return True


def _keyed_counts(
    conn: duckdb.DuckDBPyConnection, key: tuple[str, ...], common: list[str]
) -> tuple[int, int, int]:
    on = " AND ".join(f"b.{quote_ident(k)} = a.{quote_ident(k)}" for k in key)
    added = _scalar(
        conn,
        f"SELECT count(*) FROM after_t a WHERE NOT EXISTS (SELECT 1 FROM before_t b WHERE {on})",
    )
    deleted = _scalar(
        conn,
        f"SELECT count(*) FROM before_t b WHERE NOT EXISTS (SELECT 1 FROM after_t a WHERE {on})",
    )
    value_cols = [c for c in common if c not in key]
    if not value_cols:
        return added, deleted, 0
    differs = " OR ".join(
        f"b.{quote_ident(c)} IS DISTINCT FROM a.{quote_ident(c)}" for c in value_cols
    )
    modified = _scalar(
        conn, f"SELECT count(*) FROM before_t b JOIN after_t a ON {on} WHERE {differs}"
    )
    return added, deleted, modified


def _multiset_counts(
    conn: duckdb.DuckDBPyConnection, common: list[str], before: pa.Table, after: pa.Table
) -> tuple[int, int, None]:
    if not common:
        return after.num_rows, before.num_rows, None
    cols = ", ".join(quote_ident(c) for c in common)
    deleted = _scalar(
        conn,
        f"SELECT count(*) FROM (SELECT {cols} FROM before_t EXCEPT ALL SELECT {cols} FROM after_t)",
    )
    added = _scalar(
        conn,
        f"SELECT count(*) FROM (SELECT {cols} FROM after_t EXCEPT ALL SELECT {cols} FROM before_t)",
    )
    return added, deleted, None
