"""Impact of a change: compare a table before and after, as rows and schema.

Rows are compared in DuckDB. With a usable unique key, rows are matched by key and split into
added / deleted / modified. Without one, whole rows are compared as multisets: a modified row
shows up as one deleted plus one added row, and `rows_modified` is None.

`rows_affected` is the number of pre-existing rows the change deleted or rewrote, which is what
the policy's blast-radius thresholds measure. Pure inserts therefore affect zero rows.

`compute_row_diff` goes further: it lists the changed rows (for a reviewer) and fingerprints them.
It reads the base and head snapshots of an Iceberg table, always in that direction, and matches
rows on `table_key`: the table's Iceberg identifier fields, else the policy's `unique_key`, else
no key (rows compared as multisets, labelled "unkeyed"). The whole of both snapshots is
compared: no part of a statement is used to narrow the scans, because the engine and Arrow
disagree on NaN ordering and float coercion, and a row the filter dropped would be a row the
reviewer never saw. A table above the row or byte cap is refused (`DiffTooLarge`) rather than
half-read, and `check_against_impact` refuses a diff whose counts differ from the measured
impact: a reviewer must never approve a diff that was cut short.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

import duckdb
import pyarrow as pa
from pyiceberg.table import Table

from sqlguard.safety.classifier import Impact
from sqlguard.safety.store import canonical_json, digest


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


# -- row-level diff ------------------------------------------------------------------------------

DEFAULT_MAX_DIFF_ROWS = 1_000_000
DEFAULT_MAX_DIFF_BYTES = 256 * 1024 * 1024
DEFAULT_DISPLAY_ROWS = 200
_PRESENT = "__sqlguard_present"
_BATCH = 4096


class DiffError(RuntimeError):
    """The row diff could not be computed. Callers treat that as 'cannot be reviewed'."""


class DiffTooLarge(DiffError):
    """A side of the diff is above the row or byte cap, so it was not read."""


@dataclass(frozen=True)
class DiffLimits:
    """Caps on what one diff may read, per snapshot, and how many changed rows it lists."""

    max_rows: int = DEFAULT_MAX_DIFF_ROWS
    max_bytes: int = DEFAULT_MAX_DIFF_BYTES
    display_rows: int = DEFAULT_DISPLAY_ROWS


@dataclass(frozen=True)
class TableKey:
    """The columns that identify a row, and where they came from."""

    columns: tuple[str, ...] = ()
    source: Literal["identifier", "policy", "none"] = "none"


def table_key(table: Table, configured: Sequence[str] = ()) -> TableKey:
    """The key to match rows on: Iceberg identifier fields, else `configured`, else none.

    `configured` is the policy's `unique_key` for the table. The Iceberg identifier fields win
    because the table's owner declared them; the policy is the fallback for tables without any.
    """
    schema = table.schema()
    names = tuple(schema.find_column_name(i) for i in schema.identifier_field_ids)
    if names and all(names):
        return TableKey(tuple(n for n in names if n), "identifier")
    if configured:
        return TableKey(tuple(configured), "policy")
    return TableKey()


@dataclass(frozen=True)
class RowChange:
    """One changed row. `before` / `after` map column to value; `key` is None when unkeyed."""

    change: Literal["added", "deleted", "modified"]
    key: dict[str, Any] | None
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    changed_columns: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "change": self.change,
            "key": _plain(self.key),
            "before": _plain(self.before),
            "after": _plain(self.after),
            "changed_columns": list(self.changed_columns),
        }


@dataclass(frozen=True)
class RowDiff:
    """Row-level difference between two snapshots of one table, base to head.

    `digest` fingerprints the whole set of changed rows (not only the listed ones), so two diffs
    with the same digest changed the same rows in the same way. `rows_modified` is None for an
    unkeyed diff, where a modified row is one deleted plus one added row.
    """

    table: str
    base_snapshot_id: int | None
    head_snapshot_id: int | None
    mode: Literal["keyed", "unkeyed"]
    key: tuple[str, ...]
    key_source: str
    key_note: str | None
    rows: tuple[RowChange, ...]
    rows_added: int
    rows_deleted: int
    rows_modified: int | None
    display_limit: int
    schema_diff: SchemaDiff
    digest: str

    @property
    def rows_changed(self) -> int:
        return self.rows_added + self.rows_deleted + (self.rows_modified or 0)

    @property
    def truncated(self) -> bool:
        return self.rows_changed > len(self.rows)

    def to_dict(self, *, include_rows: bool = True) -> dict[str, Any]:
        """JSON-friendly; `include_rows=False` leaves out every row value."""
        out: dict[str, Any] = {
            "table": self.table,
            "base_snapshot_id": self.base_snapshot_id,
            "head_snapshot_id": self.head_snapshot_id,
            "mode": self.mode,
            "key": list(self.key),
            "key_source": self.key_source,
            "key_note": self.key_note,
            "rows_added": self.rows_added,
            "rows_deleted": self.rows_deleted,
            "rows_modified": self.rows_modified,
            "rows_changed": self.rows_changed,
            "truncated": self.truncated,
            "display_limit": self.display_limit,
            "schema_diff": self.schema_diff.to_dict(),
            "digest": self.digest,
        }
        if include_rows:
            out["rows"] = [r.to_dict() for r in self.rows]
        return out


def _plain(value: Any) -> Any:
    """Row values as JSON-safe plain data."""
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).hex()
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def _differs(left: Any, right: Any) -> bool:
    """SQL `IS DISTINCT FROM` for display: NULLs are equal to each other, and so are NaNs."""
    if left is None or right is None:
        return left is not right
    if isinstance(left, float) and isinstance(right, float) and left != left and right != right:
        return False
    return bool(left != right)


def row_diff(
    before: pa.Table,
    after: pa.Table,
    key: Sequence[str] = (),
    *,
    table: str = "",
    base_snapshot_id: int | None = None,
    head_snapshot_id: int | None = None,
    key_source: str = "none",
    display_rows: int = DEFAULT_DISPLAY_ROWS,
) -> RowDiff:
    """Diff two Arrow tables of the same table, `before` to `after`.

    Columns present on both sides are compared; a column that exists on one side only is
    reported in `schema_diff`. A key that is missing, has NULLs or repeats a value on either
    side is not used (see `key_note`); the diff then falls back to comparing whole rows.
    """
    common = [n for n in before.column_names if n in after.column_names]
    schema = schema_diff(before.schema, after.schema)
    conn = duckdb.connect()
    try:
        conn.register("before_t", before)
        conn.register("after_t", after)
        usable: tuple[str, ...] | None = None
        note: str | None = None
        if key:
            if _key_usable(conn, key, common):
                usable = tuple(key)
            else:
                note = f"key ({', '.join(key)}) is missing, null or repeated; rows compared whole"
        if not common:
            changes: list[RowChange] = []
            counts = {"added": after.num_rows, "deleted": before.num_rows}
            fingerprint = digest(counts)
            modified = None
        else:
            changes, counts, fingerprint = _collect_changes(conn, common, usable, display_rows)
            modified = counts.get("modified", 0) if usable else None
    except duckdb.Error as err:
        raise DiffError(f"cannot diff {table or 'table'}: {err}") from err
    finally:
        conn.close()
    added, deleted = counts.get("added", 0), counts.get("deleted", 0)
    return RowDiff(
        table=table,
        base_snapshot_id=base_snapshot_id,
        head_snapshot_id=head_snapshot_id,
        mode="keyed" if usable else "unkeyed",
        key=usable or (),
        key_source=key_source if usable else "none",
        key_note=note,
        rows=tuple(changes),
        rows_added=added,
        rows_deleted=deleted,
        rows_modified=modified,
        display_limit=display_rows,
        schema_diff=schema,
        digest=digest(
            {
                "table": table,
                "base": base_snapshot_id,
                "head": head_snapshot_id,
                "mode": "keyed" if usable else "unkeyed",
                "key": list(usable or ()),
                "added": added,
                "deleted": deleted,
                "modified": modified,
                "schema": schema.to_dict(),
                "rows": fingerprint,
            }
        ),
    )


def _collect_changes(
    conn: duckdb.DuckDBPyConnection,
    common: list[str],
    key: tuple[str, ...] | None,
    display_rows: int,
) -> tuple[list[RowChange], dict[str, int], str]:
    """Run the diff query once, in a total order: list the first rows, count and hash them all."""
    before_cols = ", ".join(f'b.{quote_ident(c)} AS "b{i}"' for i, c in enumerate(common))
    after_cols = ", ".join(f'a.{quote_ident(c)} AS "a{i}"' for i, c in enumerate(common))
    if key:
        key_cols = ", ".join(
            f'COALESCE(b.{quote_ident(k)}, a.{quote_ident(k)}) AS "k{j}"' for j, k in enumerate(key)
        )
        on = " AND ".join(f"b.{quote_ident(k)} = a.{quote_ident(k)}" for k in key)
        values = [c for c in common if c not in key]
        differs = "".join(
            f" OR b.{quote_ident(c)} IS DISTINCT FROM a.{quote_ident(c)}" for c in values
        )
        query = (
            f"SELECT CASE WHEN b.{_PRESENT} IS NULL THEN 'added' "
            f"WHEN a.{_PRESENT} IS NULL THEN 'deleted' ELSE 'modified' END AS change, "
            f"{key_cols}, {before_cols}, {after_cols} "
            f"FROM (SELECT *, 1 AS {_PRESENT} FROM before_t) b "
            f"FULL OUTER JOIN (SELECT *, 1 AS {_PRESENT} FROM after_t) a ON {on} "
            f"WHERE b.{_PRESENT} IS NULL OR a.{_PRESENT} IS NULL{differs} "
            f"ORDER BY {', '.join(f'"k{j}"' for j in range(len(key)))}"
        )
    else:
        cols = ", ".join(quote_ident(c) for c in common)
        nulls_b = ", ".join(f'NULL AS "b{i}"' for i in range(len(common)))
        nulls_a = ", ".join(f'NULL AS "a{i}"' for i in range(len(common)))
        deleted_cols = ", ".join(f'{quote_ident(c)} AS "b{i}"' for i, c in enumerate(common))
        added_cols = ", ".join(f'{quote_ident(c)} AS "a{i}"' for i, c in enumerate(common))
        order = ", ".join(f'"b{i}", "a{i}"' for i in range(len(common)))
        query = (
            f"SELECT * FROM ("
            f"SELECT 'deleted' AS change, {deleted_cols}, {nulls_a} FROM "
            f"(SELECT {cols} FROM before_t EXCEPT ALL SELECT {cols} FROM after_t) "
            f"UNION ALL "
            f"SELECT 'added' AS change, {nulls_b}, {added_cols} FROM "
            f"(SELECT {cols} FROM after_t EXCEPT ALL SELECT {cols} FROM before_t)"
            f") ORDER BY change, {order}"
        )
    width = len(common)
    offset = 1 + (len(key) if key else 0)
    cursor = conn.execute(query)
    hasher = hashlib.sha256()
    counts: dict[str, int] = {}
    shown: list[RowChange] = []
    while batch := cursor.fetchmany(_BATCH):
        for row in batch:
            change = str(row[0])
            counts[change] = counts.get(change, 0) + 1
            hasher.update(canonical_json(list(row)).encode("utf-8"))
            hasher.update(b"\n")
            if len(shown) < display_rows:
                old = dict(zip(common, row[offset : offset + width], strict=True))
                new = dict(zip(common, row[offset + width :], strict=True))
                shown.append(_row_change(change, common, key, row, old, new))
    return shown, counts, hasher.hexdigest()


def _row_change(
    change: str,
    common: list[str],
    key: tuple[str, ...] | None,
    row: tuple[Any, ...],
    old: dict[str, Any],
    new: dict[str, Any],
) -> RowChange:
    identity = dict(zip(key, row[1 : 1 + len(key)], strict=True)) if key else None
    if change == "added":
        return RowChange("added", identity, None, new)
    if change == "deleted":
        return RowChange("deleted", identity, old, None)
    changed = tuple(c for c in common if _differs(old[c], new[c]))
    return RowChange("modified", identity, old, new, changed)


# -- reading snapshots, with caps -----------------------------------------------------------------


def _read_snapshot(table: Table, snapshot_id: int, limits: DiffLimits) -> pa.Table:
    if table.snapshot_by_id(snapshot_id) is None:
        raise DiffError(f"snapshot {snapshot_id} of {'.'.join(table.name())} no longer exists")
    scan = table.scan(snapshot_id=snapshot_id)
    rows = size = 0
    for task in scan.plan_files():
        rows += task.file.record_count
        size += task.file.file_size_in_bytes
        if rows > limits.max_rows or size > limits.max_bytes:
            break
    if rows > limits.max_rows or size > limits.max_bytes:
        raise DiffTooLarge(
            f"table too large to diff: snapshot {snapshot_id} has at least {rows} rows / "
            f"{size} bytes to read (limits {limits.max_rows} rows / {limits.max_bytes} bytes)"
        )
    data = scan.to_arrow()
    if data.nbytes > limits.max_bytes or data.num_rows > limits.max_rows:
        raise DiffTooLarge(
            f"table too large to diff: snapshot {snapshot_id} read as {data.num_rows} rows / "
            f"{data.nbytes} bytes (limits {limits.max_rows} rows / {limits.max_bytes} bytes)"
        )
    return data


def compute_row_diff(
    table: Table,
    base_snapshot_id: int,
    head_snapshot_id: int,
    *,
    key: TableKey | Sequence[str] = (),
    limits: DiffLimits | None = None,
) -> RowDiff:
    """Row diff of `table` from snapshot `base_snapshot_id` to `head_snapshot_id`.

    Both snapshots are read whole. Raises DiffTooLarge above the caps, and DiffError for
    anything else that stops the diff (missing snapshot, incomparable column types).
    """
    limits = limits or DiffLimits()
    chosen = key if isinstance(key, TableKey) else TableKey(tuple(key), "policy" if key else "none")
    try:
        before = _read_snapshot(table, base_snapshot_id, limits)
        after = _read_snapshot(table, head_snapshot_id, limits)
    except DiffError:
        raise
    except Exception as err:  # noqa: BLE001 - PyIceberg raises I/O and catalog errors of many kinds
        raise DiffError(f"cannot read {'.'.join(table.name())} for the diff: {err}") from err
    return row_diff(
        before,
        after,
        chosen.columns,
        table=".".join(table.name()),
        base_snapshot_id=base_snapshot_id,
        head_snapshot_id=head_snapshot_id,
        key_source=chosen.source,
        display_rows=limits.display_rows,
    )


def check_size(before: pa.Table, after: pa.Table, limits: DiffLimits) -> None:
    """Raise DiffTooLarge unless both sides are within the caps (for data already in memory)."""
    for side in (before, after):
        if side.num_rows > limits.max_rows or side.nbytes > limits.max_bytes:
            raise DiffTooLarge(
                f"table too large to diff: {side.num_rows} rows / {side.nbytes} bytes "
                f"(limits {limits.max_rows} rows / {limits.max_bytes} bytes)"
            )


def check_against_impact(diff: RowDiff, impact: TableImpact) -> None:
    """Raise DiffError unless the listed diff accounts for exactly what the impact measured.

    The net row change must always agree; when both were matched on the same key (or both
    unkeyed) the added, deleted and modified counts must too. A disagreement means the diff is
    not the whole change, so it cannot be offered for review.
    """
    if diff.rows_added - diff.rows_deleted != impact.rows_after - impact.rows_before:
        raise DiffError("the row diff does not add up to the measured row count change")
    if tuple(impact.key or ()) == diff.key and (
        diff.rows_added,
        diff.rows_deleted,
        diff.rows_modified,
    ) != (impact.rows_added, impact.rows_deleted, impact.rows_modified):
        raise DiffError("the row diff disagrees with the measured impact")
