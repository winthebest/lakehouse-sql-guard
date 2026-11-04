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
