"""Row-level diff between two snapshots: keys, caps, determinism, and WHERE pushdown."""

from __future__ import annotations

import math

import pyarrow as pa
import pytest

from sqlguard.core.catalog import LakehouseCatalog
from sqlguard.safety import (
    DiffError,
    DiffLimits,
    DiffTooLarge,
    TableKey,
    compute_row_diff,
    row_diff,
    statement_filter,
    table_key,
)

SCHEMA = pa.schema(
    [
        pa.field("id", pa.int64(), nullable=False),
        pa.field("region", pa.string()),
        pa.field("amount", pa.float64()),
    ]
)


def rows(*items: tuple[int, str | None, float | None]) -> pa.Table:
    return pa.table(
        {
            "id": pa.array([i[0] for i in items], pa.int64()),
            "region": pa.array([i[1] for i in items], pa.string()),
            "amount": pa.array([i[2] for i in items], pa.float64()),
        },
        schema=SCHEMA,
    )


BASE = [(1, "eu", 10.0), (2, "eu", 20.0), (3, "us", 30.0), (4, "us", 40.0)]


@pytest.fixture
def t(catalog: LakehouseCatalog):
    """`db.t` (4 rows) keyed by the Iceberg identifier field `id`; returns (table, base id)."""
    catalog.create_table("t", rows(*BASE))
    iceberg = catalog.table("t")
    with iceberg.update_schema() as update:
        update.set_identifier_fields("id")
    iceberg = catalog.table("t")
    return iceberg, iceberg.current_snapshot().snapshot_id


def overwrite(catalog: LakehouseCatalog, data: pa.Table) -> int:
    catalog.table("t").overwrite(data)
    return catalog.table("t").current_snapshot().snapshot_id


def changed(items, *, delete=(), update=None, add=()):
    update = update or {}
    out = [(i, r, update.get(i, a)) for i, r, a in items if i not in delete]
    return rows(*out, *add)


# -- keys ------------------------------------------------------------------------------------


def test_identifier_fields_are_the_first_choice_of_key(t):
    iceberg, _ = t
    assert table_key(iceberg, ["region"]) == TableKey(("id",), "identifier")


def test_policy_key_is_the_fallback_and_none_is_the_last(catalog):
    catalog.create_table("plain", rows(*BASE))
    iceberg = catalog.table("plain")
    assert table_key(iceberg, ["id"]) == TableKey(("id",), "policy")
    assert table_key(iceberg) == TableKey((), "none")


def test_keyed_diff_splits_added_deleted_and_modified(catalog, t):
    iceberg, base = t
    head = overwrite(
        catalog,
        changed(BASE, delete={1}, update={2: 21.0}, add=[(9, "ap", 90.0)]),
    )
    diff = compute_row_diff(catalog.table("t"), base, head, key=table_key(iceberg))
    assert (diff.mode, diff.key, diff.key_source) == ("keyed", ("id",), "identifier")
    assert (diff.rows_added, diff.rows_deleted, diff.rows_modified) == (1, 1, 1)
    by_change = {r.change: r for r in diff.rows}
    assert by_change["added"].after == {"id": 9, "region": "ap", "amount": 90.0}
    assert by_change["added"].before is None and by_change["added"].key == {"id": 9}
    assert by_change["deleted"].before["id"] == 1 and by_change["deleted"].after is None
    assert by_change["modified"].changed_columns == ("amount",)
    assert by_change["modified"].before["amount"] == 20.0
    assert by_change["modified"].after["amount"] == 21.0
    assert not diff.truncated and diff.rows_changed == 3


def test_diff_always_reads_base_to_head(catalog, t):
    iceberg, base = t
    head = overwrite(catalog, changed(BASE, delete={1}))
    forward = compute_row_diff(catalog.table("t"), base, head, key=("id",))
    assert (forward.rows_added, forward.rows_deleted) == (0, 1)
    backward = compute_row_diff(catalog.table("t"), head, base, key=("id",))
    assert (backward.rows_added, backward.rows_deleted) == (1, 0)
    assert forward.digest != backward.digest


def test_unkeyed_diff_compares_whole_rows_and_says_so(catalog):
    catalog.create_table("t", rows(*BASE))
    base = catalog.table("t").current_snapshot().snapshot_id
    head = overwrite(catalog, changed(BASE, update={2: 21.0}))
    diff = compute_row_diff(catalog.table("t"), base, head)
    assert (diff.mode, diff.key, diff.key_source) == ("unkeyed", (), "none")
    assert diff.rows_modified is None  # one deleted plus one added row instead
    assert (diff.rows_added, diff.rows_deleted) == (1, 1)
    assert {r.change for r in diff.rows} == {"added", "deleted"}
    assert all(r.key is None for r in diff.rows)


def test_unkeyed_diff_counts_duplicates_as_a_multiset():
    two = rows((1, "a", 1.0), (1, "a", 1.0))
    one = rows((1, "a", 1.0))
    diff = row_diff(two, one)
    assert (diff.rows_added, diff.rows_deleted) == (0, 1)


def test_composite_key(catalog):
    base_rows = [(1, "eu", 1.0), (1, "us", 2.0), (2, "eu", 3.0)]
    catalog.create_table("t", rows(*base_rows))
    base = catalog.table("t").current_snapshot().snapshot_id
    head = overwrite(catalog, rows((1, "eu", 1.0), (1, "us", 2.5), (2, "eu", 3.0), (2, "us", 4.0)))
    diff = compute_row_diff(catalog.table("t"), base, head, key=("id", "region"))
    assert diff.mode == "keyed" and diff.key == ("id", "region")
    assert (diff.rows_added, diff.rows_deleted, diff.rows_modified) == (1, 0, 1)
    modified = next(r for r in diff.rows if r.change == "modified")
    assert modified.key == {"id": 1, "region": "us"}


def test_a_key_that_repeats_or_is_null_is_not_used(catalog):
    repeated = rows((1, "a", 1.0), (1, "b", 2.0))
    other = rows((1, "a", 1.0), (1, "b", 3.0))
    diff = row_diff(repeated, other, ["id"])
    assert diff.mode == "unkeyed" and diff.key == ()
    assert "repeated" in (diff.key_note or "")
    nullable = pa.table({"id": pa.array([1, None], pa.int64()), "v": [1, 2]})
    assert row_diff(nullable, nullable, ["id"]).mode == "unkeyed"
    missing = row_diff(repeated, other, ["nope"])
    assert missing.mode == "unkeyed" and missing.key_note


def test_null_and_nan_compare_as_equal_values(catalog):
    a = rows((1, None, math.nan), (2, "x", 1.0))
    b = rows((1, None, math.nan), (2, "x", 1.0))
    assert row_diff(a, b, ["id"]).rows_changed == 0
    c = rows((1, "now-set", math.nan), (2, "x", 1.0))
    diff = row_diff(a, c, ["id"])
    assert diff.rows_modified == 1 and diff.rows[0].changed_columns == ("region",)


def test_columns_present_on_one_side_are_reported_not_compared():
    old = pa.table({"id": pa.array([1, 2], pa.int64()), "a": [1, 2]})
    new = pa.table({"id": pa.array([1, 2], pa.int64()), "a": [1, 3], "b": ["x", "y"]})
    diff = row_diff(old, new, ["id"])
    assert diff.schema_diff.added == ("b",) and diff.rows_modified == 1
    assert diff.rows[0].after.keys() == {"id", "a"}


def test_tables_with_no_common_column_are_all_added_and_deleted():
    diff = row_diff(pa.table({"a": [1, 2]}), pa.table({"b": [1, 2, 3]}))
    assert (diff.rows_added, diff.rows_deleted, diff.rows) == (3, 2, ())


# -- display and digest ----------------------------------------------------------------------


def test_only_a_page_of_rows_is_listed_but_the_totals_and_digest_cover_everything():
    before = pa.table({"id": pa.array(range(500), pa.int64()), "v": pa.array(range(500))})
    after = pa.table({"id": pa.array(range(500), pa.int64()), "v": pa.array(range(1, 501))})
    diff = row_diff(before, after, ["id"])
    assert len(diff.rows) == 200 and diff.rows_modified == 500
    assert diff.truncated and diff.display_limit == 200
    assert [r.key["id"] for r in diff.rows] == list(range(200))  # ordered by key
    # a change beyond the listed page still changes the digest
    tweaked = after.set_column(1, "v", pa.array([*range(1, 500), 9999]))
    assert row_diff(before, tweaked, ["id"]).digest != diff.digest
    assert row_diff(before, after, ["id"]).digest == diff.digest


def test_digest_does_not_depend_on_row_order_or_page_size():
    a = pa.table({"id": pa.array([1, 2, 3], pa.int64()), "v": [1, 2, 3]})
    b = pa.table({"id": pa.array([3, 1, 2], pa.int64()), "v": [30, 1, 2]})
    c = pa.table({"id": pa.array([1, 2, 3], pa.int64()), "v": [1, 2, 30]})
    assert row_diff(a, b, ["id"]).digest == row_diff(a, c, ["id"]).digest
    assert row_diff(a, c, ["id"], display_rows=1).digest == row_diff(a, c, ["id"]).digest
    assert len(row_diff(a, c, ["id"], display_rows=0).rows) == 0


def test_to_dict_is_json_safe_and_can_leave_values_out(catalog, t):
    import datetime as dt
    import decimal
    import json

    wide = pa.table(
        {
            "id": pa.array([1], pa.int64()),
            "ts": pa.array([dt.datetime(2026, 1, 1)], pa.timestamp("us")),
            "price": pa.array([decimal.Decimal("1.50")], pa.decimal128(5, 2)),
            "raw": pa.array([b"\x01"], pa.binary()),
        }
    )
    other = wide.set_column(0, "id", pa.array([2], pa.int64()))
    diff = row_diff(wide, other, ["id"])
    text = json.dumps(diff.to_dict())
    assert "2026-01-01" in text and '"1.50"' in text and "01" in text
    bare = diff.to_dict(include_rows=False)
    assert "rows" not in bare and "2026" not in json.dumps(bare)


# -- caps ------------------------------------------------------------------------------------


def test_a_table_above_the_row_cap_is_refused_not_truncated(catalog, t):
    iceberg, base = t
    head = overwrite(catalog, changed(BASE, delete={1}))
    with pytest.raises(DiffTooLarge, match="table too large to diff"):
        compute_row_diff(catalog.table("t"), base, head, limits=DiffLimits(max_rows=3))


def test_a_table_above_the_byte_cap_is_refused(catalog, t):
    iceberg, base = t
    head = overwrite(catalog, changed(BASE, delete={1}))
    with pytest.raises(DiffTooLarge, match="bytes"):
        compute_row_diff(catalog.table("t"), base, head, limits=DiffLimits(max_bytes=10))


def test_within_caps_the_same_diff_succeeds(catalog, t):
    iceberg, base = t
    head = overwrite(catalog, changed(BASE, delete={1}))
    limits = DiffLimits(max_rows=4, max_bytes=10_000_000)
    assert compute_row_diff(catalog.table("t"), base, head, limits=limits).rows_deleted == 1


def test_a_missing_snapshot_is_an_error_not_an_empty_diff(catalog, t):
    iceberg, base = t
    with pytest.raises(DiffError, match="no longer exists"):
        compute_row_diff(catalog.table("t"), base, 12345)


# -- pushdown --------------------------------------------------------------------------------

STATEMENTS = [
    ("DELETE FROM t WHERE id = 1", True),
    ("DELETE FROM t WHERE region = 'us' AND amount > 30", True),
    ("DELETE FROM t WHERE id IN (1, 3) OR region = 'eu'", True),
    ("DELETE FROM t WHERE id BETWEEN 2 AND 3", True),
    ("DELETE FROM t WHERE region IS NOT NULL AND amount >= 20.5", True),
    ("DELETE FROM t WHERE 2 < id", True),
    ("UPDATE t SET amount = amount + 1 WHERE region = 'eu'", True),
    ("UPDATE t SET amount = 0 WHERE id = 2", True),
    ("DELETE FROM t AS x WHERE x.id = 1", True),
    # assigns a column the WHERE clause reads: a rewritten row could leave the filter
    ("UPDATE t SET region = 'ap' WHERE region = 'eu'", False),
    ("UPDATE t SET id = id + 10 WHERE id < 3", False),
    ("DELETE FROM t WHERE NOT (id = 1)", False),
    ("DELETE FROM t WHERE lower(region) = 'eu'", False),
    ("DELETE FROM t WHERE id IN (SELECT id FROM t)", False),
    ("DELETE FROM t WHERE amount = 'x'", False),  # string literal against a double column
    ("DELETE FROM t WHERE id = 1.5", False),  # fractional literal against an integer column
    ("DELETE FROM t WHERE region = 1", False),
    ("DELETE FROM t", False),  # no WHERE
    ("INSERT INTO t VALUES (7, 'x', 1.0)", False),
    ("DELETE FROM t WHERE id = 1; DELETE FROM t WHERE id = 2", False),
    ("DELETE FROM t WHERE other.id = 1", False),
    ("DELETE FROM t WHERE nope = 1", False),
    ("not sql at all (", False),
]


@pytest.mark.parametrize(("sql", "pushed"), STATEMENTS)
def test_which_statements_are_pushed_down(t, sql, pushed):
    iceberg, _ = t
    assert (statement_filter(sql, "duckdb", iceberg.schema()) is not None) is pushed


@pytest.mark.parametrize(
    "sql",
    [sql for sql, pushed in STATEMENTS if pushed]
    + [
        "UPDATE t SET region = 'ap' WHERE region = 'eu'",
        "UPDATE t SET id = id + 10 WHERE id < 3",
        "DELETE FROM t WHERE NOT (id = 1)",
    ],
)
def test_pushdown_never_changes_the_diff(catalog, t, sql):
    """The filtered diff has the same rows, counts and digest as the unfiltered one."""
    from sqlguard.safety.executor import DuckDBIcebergAdapter

    iceberg, base = t
    adapter = DuckDBIcebergAdapter(catalog)
    branch = adapter.create_branch("t")
    adapter.apply_sql(branch, sql)
    head = adapter.diff(branch).branch.head
    table = catalog.table("t")
    plain = compute_row_diff(table, base, head, key=table_key(table))
    flt = statement_filter(sql, "duckdb", table.schema())
    pushed = compute_row_diff(table, base, head, key=table_key(table), row_filter=flt)
    assert pushed.digest == plain.digest
    assert [r.to_dict() for r in pushed.rows] == [r.to_dict() for r in plain.rows]
    assert (pushed.rows_added, pushed.rows_deleted, pushed.rows_modified) == (
        plain.rows_added,
        plain.rows_deleted,
        plain.rows_modified,
    )


def test_pushdown_reads_fewer_rows_and_is_reported(catalog):
    big = pa.table(
        {
            "id": pa.array(range(1000), pa.int64()),
            "bucket": pa.array([i // 100 for i in range(1000)], pa.int64()),
        }
    )
    iceberg = catalog.create_table("t", big)
    # one file per bucket, so file-level pruning applies
    iceberg.overwrite(big.slice(0, 0))
    for bucket in range(10):
        catalog.table("t").append(big.filter(pa.compute.equal(big["bucket"], bucket)))
    table = catalog.table("t")
    base = table.current_snapshot().snapshot_id
    table.delete("id = 5")
    head = catalog.table("t").current_snapshot().snapshot_id
    table = catalog.table("t")
    flt = statement_filter("DELETE FROM t WHERE id = 5", "duckdb", table.schema())
    pushed = compute_row_diff(table, base, head, key=("id",), row_filter=flt)
    plain = compute_row_diff(table, base, head, key=("id",))
    assert pushed.digest == plain.digest and pushed.rows_deleted == 1
    assert plain.scanned_rows[0] == 1000
    assert pushed.scanned_rows[0] < plain.scanned_rows[0]
    assert pushed.pushdown and plain.pushdown is None


def test_pushdown_lets_a_big_table_through_the_cap(catalog):
    data = pa.table({"id": pa.array(range(400), pa.int64())})
    catalog.create_table("t", data.slice(0, 0))
    for start in range(0, 400, 100):
        catalog.table("t").append(data.slice(start, 100))
    table = catalog.table("t")
    base = table.current_snapshot().snapshot_id
    table.delete("id = 5")
    table = catalog.table("t")
    head = table.current_snapshot().snapshot_id
    limits = DiffLimits(max_rows=150)
    with pytest.raises(DiffTooLarge):
        compute_row_diff(table, base, head, limits=limits)
    flt = statement_filter("DELETE FROM t WHERE id = 5", "duckdb", table.schema())
    assert compute_row_diff(table, base, head, row_filter=flt, limits=limits).rows_deleted == 1
