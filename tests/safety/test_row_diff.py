"""Row-level diff between two snapshots: keys, caps, determinism, and the whole-table comparison."""

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


# -- no part of a statement narrows the comparison ----------------------------------------------


def test_the_whole_table_is_compared_whatever_the_statement(catalog, t):
    """A reviewer sees every changed row: nothing is filtered by the statement's WHERE."""
    from sqlguard.safety.executor import DuckDBIcebergAdapter

    iceberg, base = t
    adapter = DuckDBIcebergAdapter(catalog)
    branch = adapter.create_branch("t")
    adapter.apply_sql(branch, "UPDATE t SET region = 'ap' WHERE region = 'eu'")
    diff = adapter.diff(branch)
    table = catalog.table("t")
    full = compute_row_diff(table, base, diff.branch.head, key=table_key(table))
    assert full.rows_modified == 2 and {r.key["id"] for r in full.rows} == {1, 2}
    assert (
        full.digest
        == row_diff(
            diff.before,
            diff.after,
            ["id"],
            table="db.t",
            base_snapshot_id=base,
            head_snapshot_id=diff.branch.head,
            key_source="identifier",
        ).digest
    )


def test_nan_rows_and_float32_values_are_diffed_like_the_engine_sees_them(catalog):
    """Rows an engine would match (NaN above every number, float32 0.1) are all listed."""
    data = pa.table(
        {
            "id": pa.array(range(6), pa.int64()),
            "f": pa.array([0.1, 0.1, 1.0, math.nan, math.nan, 20.0], pa.float32()),
        }
    )
    catalog.create_table("f", data)
    base = catalog.table("f").current_snapshot().snapshot_id
    catalog.table("f").overwrite(data.filter(pa.compute.is_in(data["id"], pa.array([2, 5]))))
    head = catalog.table("f").current_snapshot().snapshot_id
    diff = compute_row_diff(catalog.table("f"), base, head, key=("id",))
    assert diff.rows_deleted == 4 and {r.key["id"] for r in diff.rows} == {0, 1, 3, 4}


def test_check_against_impact_refuses_a_diff_that_disagrees_with_the_measured_impact():
    from dataclasses import replace

    from sqlguard.safety.impact import check_against_impact, compute_table_impact

    before = rows(*BASE)
    after = rows(*BASE[1:])
    impact = compute_table_impact("t", before, after, ["id"])
    good = row_diff(before, after, ["id"])
    check_against_impact(good, impact)  # same key, same counts: fine
    check_against_impact(row_diff(before, after), impact)  # other key mode: the net change agrees
    with pytest.raises(DiffError, match="disagrees"):
        check_against_impact(replace(good, rows_added=1, rows_deleted=2), impact)
    with pytest.raises(DiffError, match="add up"):
        check_against_impact(replace(good, rows_deleted=0), impact)


def test_check_size_refuses_data_already_in_memory():
    from sqlguard.safety.impact import check_size

    check_size(rows(*BASE), rows(*BASE), DiffLimits())
    with pytest.raises(DiffTooLarge):
        check_size(rows(*BASE), rows(*BASE), DiffLimits(max_rows=3))
    with pytest.raises(DiffTooLarge):
        check_size(rows(*BASE), rows(*BASE), DiffLimits(max_bytes=10))


# -- types that cannot be turned into Python objects without extra modules ----------------------


def wide(shift: int) -> pa.Table:
    import datetime as dt
    import decimal

    base = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)

    def stamps(tz: str):
        return pa.array(
            [base + dt.timedelta(seconds=shift * i) for i in range(3)], pa.timestamp("us", tz=tz)
        )

    return pa.table(
        {
            "id": pa.array([1, 2, 3], pa.int64()),
            "utc": stamps("UTC"),
            "new_york": stamps("America/New_York"),
            "ho_chi_minh": stamps("Asia/Ho_Chi_Minh"),
            "india": stamps("+05:30"),
            "nanos": pa.array([base.replace(tzinfo=None)] * 3, pa.timestamp("ns")),
            "dec": pa.array([decimal.Decimal("1.50") + shift] * 3, pa.decimal128(10, 2)),
            "nested": pa.array(
                [{"a": i, "t": base + dt.timedelta(seconds=shift)} for i in range(3)],
                pa.struct([("a", pa.int64()), ("t", pa.timestamp("us", tz="UTC"))]),
            ),
            "items": pa.array([[1, 2], [3], [shift]], pa.list_(pa.int64())),
            "attrs": pa.array([[("k", shift)], [], [("z", 2)]], pa.map_(pa.string(), pa.int64())),
        }
    )


@pytest.mark.parametrize("key", [["id"], []])
def test_timezone_decimal_and_nested_columns_are_diffed_without_python_conversion(key):
    diff = row_diff(wide(0), wide(1), key)
    if key:
        assert diff.mode == "keyed" and diff.rows_modified == 3
        changed = {c for r in diff.rows for c in r.changed_columns}
        assert {
            "utc",
            "new_york",
            "ho_chi_minh",
            "india",
            "dec",
            "nested",
            "items",
            "attrs",
        } <= changed
        assert "nanos" not in changed and "id" not in changed
    else:
        assert diff.mode == "unkeyed" and diff.rows_added == diff.rows_deleted == 3
    shown = str([r.to_dict() for r in diff.rows])
    assert "2026-01-01 00:00:01+00" in shown  # one instant, printed the same for every zone
    assert diff.digest == row_diff(wide(0), wide(1), key).digest
    assert row_diff(wide(0), wide(0), key).rows_changed == 0


def test_the_text_of_a_timestamptz_does_not_depend_on_the_host_time_zone(monkeypatch):
    import time

    first = row_diff(wide(0), wide(1), ["id"]).digest
    monkeypatch.setenv("TZ", "Pacific/Auckland")
    time.tzset()
    try:
        assert row_diff(wide(0), wide(1), ["id"]).digest == first
    finally:
        monkeypatch.undo()
        time.tzset()


def test_a_timezone_key_column_works(catalog):
    import datetime as dt

    base = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    before = pa.table(
        {
            "at": pa.array([base, base + dt.timedelta(hours=1)], pa.timestamp("us", tz="UTC")),
            "v": [1, 2],
        }
    )
    after = before.set_column(1, "v", pa.array([1, 3]))
    diff = row_diff(before, after, ["at"])
    assert diff.mode == "keyed" and diff.rows_modified == 1
    assert diff.rows[0].key == {"at": "2026-01-01 01:00:00+00"}


def test_negative_zero_is_not_a_change_and_this_is_documented():
    """DuckDB, the impact and the diff all treat 0.0 and -0.0 as equal (documented limit)."""
    zero = pa.table({"id": pa.array([1], pa.int64()), "x": pa.array([0.0])})
    negative = pa.table({"id": pa.array([1], pa.int64()), "x": pa.array([-0.0])})
    assert row_diff(zero, negative, ["id"]).rows_changed == 0
    assert row_diff(zero, negative).rows_changed == 0
