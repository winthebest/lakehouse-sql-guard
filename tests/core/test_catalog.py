import pyarrow as pa

from sqlguard.core.catalog import LakehouseCatalog


def test_schema_for_maps_iceberg_types(orders_catalog: LakehouseCatalog):
    assert orders_catalog.schema_for() == {
        "orders": {
            "id": "BIGINT",
            "customer": "VARCHAR",
            "amount": "DOUBLE",
            "created_at": "TIMESTAMP",
        }
    }


def test_snapshots_refs_and_time_travel(orders_catalog: LakehouseCatalog):
    first = orders_catalog.snapshots("orders")[0].snapshot_id
    orders_catalog.table("orders").append(
        orders_catalog.read("orders").slice(0, 1)  # duplicate one row -> second snapshot
    )
    snaps = orders_catalog.snapshots("orders")
    assert len(snaps) == 2 and snaps[1].parent_id == first
    assert orders_catalog.refs("orders")["main"] == snaps[1].snapshot_id
    assert orders_catalog.read("orders").num_rows == 4
    assert orders_catalog.read("orders", snapshot_id=first).num_rows == 3


def test_reopen_existing_warehouse(tmp_path):
    LakehouseCatalog.local(tmp_path).create_table("t", pa.table({"a": [1]}))
    reopened = LakehouseCatalog.local(tmp_path)
    assert reopened.table_names() == ["t"] and reopened.has_table("t")


def test_logical_snapshot_time_is_normalized_to_naive_utc(tmp_path):
    from datetime import UTC, datetime

    from sqlguard.core.catalog import LOGICAL_TS_PROPERTY, naive_utc

    assert naive_utc(datetime(2026, 1, 1, 7, tzinfo=UTC)) == datetime(2026, 1, 1, 7)
    catalog = LakehouseCatalog.local(tmp_path)
    table = catalog.create_empty("t", pa.schema([("a", pa.int64())]))
    for logical in ("2026-01-01T00:00:00+00:00", "2026-01-02T07:00:00+07:00"):
        table.append(pa.table({"a": [1]}), snapshot_properties={LOGICAL_TS_PROPERTY: logical})
    visible = [s.visible_at for s in catalog.snapshots("t")]
    assert visible == [datetime(2026, 1, 1), datetime(2026, 1, 2)]
    aware = datetime(2026, 1, 1, 12, tzinfo=UTC)
    assert catalog.snapshot_as_of("t", aware) == catalog.snapshots("t")[0].snapshot_id


def test_backfill_tagged_with_an_early_logical_time_does_not_pull_later_data_forward(tmp_path):
    """A snapshot contains everything committed before it, so it is visible only once its
    ancestors are, even when its own logical time is earlier."""
    from datetime import datetime

    from sqlguard.core.catalog import LOGICAL_TS_PROPERTY

    catalog = LakehouseCatalog.local(tmp_path)
    table = catalog.create_empty("t", pa.schema([("a", pa.int64())]))
    for value, logical in (
        (1, "2026-01-01T00:00:00"),
        (2, "2026-03-01T00:00:00"),
        (3, "2026-01-15T00:00:00"),
    ):
        table.append(pa.table({"a": [value]}), snapshot_properties={LOGICAL_TS_PROPERTY: logical})
    jan, mar, backfill = (s.snapshot_id for s in catalog.snapshots("t"))

    assert catalog.snapshot_as_of("t", datetime(2026, 1, 20)) == jan
    assert catalog.snapshot_as_of("t", datetime(2026, 2, 1)) == jan  # not the backfill
    assert catalog.snapshot_as_of("t", datetime(2026, 3, 1)) == backfill
    assert sorted(
        catalog.read("t", catalog.snapshot_as_of("t", datetime(2026, 2, 1)))["a"].to_pylist()
    ) == [1]
    assert catalog.snapshot_as_of("t", datetime(2025, 12, 31)) is None
