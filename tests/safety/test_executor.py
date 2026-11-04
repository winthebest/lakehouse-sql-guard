"""Adapter behaviour, plus tests that pin the PyIceberg behaviours the adapter relies on."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pyarrow as pa
import pytest
from pyiceberg.exceptions import CommitFailedException
from pyiceberg.table.update.snapshot import ManageSnapshots

from sqlguard.safety.executor import (
    BranchDiverged,
    BranchHandle,
    DuckDBIcebergAdapter,
    ExecutionError,
    UnsupportedAction,
)
from tests.safety.conftest import ROWS, main_ids, order_ids


def one_row(order_id: int) -> pa.Table:
    return pa.table(
        {"id": pa.array([order_id], pa.int64()), "customer": ["x"], "amount": pa.array([1.0])}
    )


@pytest.fixture
def adapter(orders) -> DuckDBIcebergAdapter:
    return DuckDBIcebergAdapter(orders)


# -- PyIceberg behaviours the adapter depends on (a version bump turns these red) --------------


def test_branch_overwrite_changes_the_branch_and_leaves_main_alone(orders):
    iceberg = orders.table("orders")
    base = iceberg.current_snapshot().snapshot_id
    iceberg.manage_snapshots().create_branch(base, "b").commit()
    orders.table("orders").overwrite(one_row(1), branch="b")
    iceberg = orders.table("orders")
    assert iceberg.current_snapshot().snapshot_id == base
    assert orders.read("orders").num_rows == ROWS
    assert orders.read("orders", iceberg.refs()["b"].snapshot_id).column("id").to_pylist() == [1]


def test_rollback_only_moves_the_pointer(orders):
    first = orders.table("orders").current_snapshot().snapshot_id
    orders.table("orders").append(one_row(100))
    count = len(orders.table("orders").snapshots())
    orders.table("orders").manage_snapshots().rollback_to_snapshot(first).commit()
    iceberg = orders.table("orders")
    assert iceberg.current_snapshot().snapshot_id == first
    assert len(iceberg.snapshots()) == count  # nothing deleted, the later snapshot is just unlinked


def test_expire_snapshots_removes_metadata_but_not_parquet_files(orders):
    from pathlib import Path
    from urllib.parse import urlparse

    orders.table("orders").overwrite(one_row(1))
    iceberg = orders.table("orders")
    root = Path(urlparse(iceberg.location()).path)
    parquet_before = sorted(p.name for p in root.rglob("*.parquet"))
    iceberg.maintenance.expire_snapshots().older_than(
        datetime.now(UTC) + timedelta(days=1)
    ).commit()
    assert len(orders.table("orders").snapshots()) == 1
    assert sorted(p.name for p in root.rglob("*.parquet")) == parquet_before


def test_rolling_back_to_an_expired_snapshot_is_impossible(orders):
    first = orders.table("orders").current_snapshot().snapshot_id
    orders.table("orders").overwrite(one_row(1))
    orders.table("orders").maintenance.expire_snapshots().by_id(first).commit()
    with pytest.raises(ValueError, match="Cannot roll back to unknown snapshot id"):
        orders.table("orders").manage_snapshots().rollback_to_snapshot(first).commit()


# -- branches ----------------------------------------------------------------------------------


def test_branch_dml_publish_roundtrip(adapter, orders):
    branch = adapter.create_branch("orders")
    base = branch.base_snapshot_id
    adapter.apply_sql(
        branch, "DELETE FROM orders WHERE id <= 5; INSERT INTO orders VALUES (99, 'z', 1.0)"
    )
    assert order_ids(orders) == list(range(1, ROWS + 1))  # main untouched until publish
    diff = adapter.diff(branch)
    assert (diff.impact.rows_deleted, diff.impact.rows_added) == (5, 1)
    assert diff.before.num_rows == ROWS and diff.after.num_rows == ROWS - 4

    result = adapter.publish(branch)
    assert result.changed and result.previous_snapshot_id == base
    assert adapter.current_snapshot_id("orders") == result.snapshot_id
    assert order_ids(orders) == [*range(6, ROWS + 1), 99]
    assert main_ids(orders)[0] == base  # published history extends the original lineage


def test_publish_without_changes_does_not_move_main(adapter, orders):
    branch = adapter.create_branch("orders")
    adapter.apply_sql(branch, "UPDATE orders SET amount = amount WHERE id = 1")
    result = adapter.publish(branch)
    assert not result.changed and adapter.current_snapshot_id("orders") == branch.base_snapshot_id


def test_publish_refuses_when_main_moved(adapter, orders):
    branch = adapter.create_branch("orders")
    adapter.apply_sql(branch, "DELETE FROM orders WHERE id = 1")
    orders.table("orders").append(one_row(300))
    moved = adapter.current_snapshot_id("orders")
    with pytest.raises(BranchDiverged):
        adapter.publish(branch)
    assert adapter.current_snapshot_id("orders") == moved


def test_commit_conflict_during_publish_counts_as_divergence(adapter, orders, monkeypatch):
    branch = adapter.create_branch("orders")
    adapter.apply_sql(branch, "DELETE FROM orders WHERE id = 1")

    def conflict(self):
        raise CommitFailedException("requirement failed: branch main has changed")

    monkeypatch.setattr(ManageSnapshots, "commit", conflict)
    with pytest.raises(BranchDiverged):
        adapter.publish(branch)


def test_dropping_a_branch_is_idempotent_and_never_drops_main(adapter, orders):
    branch = adapter.create_branch("orders")
    adapter.drop_branch(branch)
    adapter.drop_branch(branch)
    assert set(orders.refs("orders")) == {"main"}
    with pytest.raises(ExecutionError):
        adapter.drop_branch(BranchHandle("db.orders", "main", 1))


def test_branch_names_are_validated_and_unique(adapter):
    with pytest.raises(ExecutionError):
        adapter.create_branch("orders", "main")
    with pytest.raises(ExecutionError):
        adapter.create_branch("orders", "bad name; drop")
    adapter.create_branch("orders", "mine")
    with pytest.raises(ExecutionError, match="already exists"):
        adapter.create_branch("orders", "mine")


def test_a_table_without_snapshots_cannot_be_branched(adapter, orders):
    orders.create_empty("fresh", pa.schema([("id", pa.int64())]))
    with pytest.raises(ExecutionError, match="no snapshot"):
        adapter.create_branch("fresh")


def test_apply_sql_rejects_non_dml_and_bad_sql(adapter):
    branch = adapter.create_branch("orders")
    with pytest.raises(UnsupportedAction):
        adapter.apply_sql(branch, "DROP TABLE orders")
    with pytest.raises(UnsupportedAction):
        adapter.apply_sql(branch, "CREATE TABLE t AS SELECT 1")
    with pytest.raises(ExecutionError):
        adapter.apply_sql(branch, "UPDATE orders SET missing_col = 1")
    with pytest.raises(ExecutionError):
        adapter.apply_sql(branch, "not sql at all")


def test_apply_sql_cannot_leave_the_registered_tables(adapter, tmp_path):
    secret = tmp_path / "secret.csv"
    secret.write_text("a\n1\n")
    branch = adapter.create_branch("orders")
    with pytest.raises(ExecutionError):
        adapter.apply_sql(branch, f"DELETE FROM orders WHERE id IN (SELECT a FROM '{secret}')")
    with pytest.raises(ExecutionError):
        adapter.query(f"SELECT * FROM read_csv('{secret}')", [])


def test_a_type_error_fails_the_branch_and_leaves_main_alone(adapter, orders):
    branch = adapter.create_branch("orders")
    with pytest.raises(ExecutionError):
        adapter.apply_sql(branch, "UPDATE orders SET id = 'text' WHERE id = 1")
    assert order_ids(orders) == list(range(1, ROWS + 1))


def test_dml_can_read_other_tables(adapter, orders):
    orders.create_table("vip", pa.table({"id": pa.array([1, 2], pa.int64())}))
    branch = adapter.create_branch("orders")
    adapter.apply_sql(
        branch, "DELETE FROM orders WHERE id IN (SELECT id FROM vip)", read_tables=["db.vip"]
    )
    assert adapter.diff(branch).impact.rows_deleted == 2


def test_query_reads_main_and_supports_qualified_names(adapter):
    result = adapter.query("SELECT count(*) AS n FROM db.orders", ["db.orders"])
    assert result.column("n").to_pylist() == [ROWS]
    with pytest.raises(UnsupportedAction):
        adapter.query("DELETE FROM orders", ["orders"])


def test_diff_with_unique_key_counts_added_deleted_and_modified(adapter):
    branch = adapter.create_branch("orders")
    adapter.apply_sql(
        branch,
        "DELETE FROM orders WHERE id = 1; UPDATE orders SET amount = 0 WHERE id IN (2, 3);"
        "INSERT INTO orders VALUES (500, 'n', 1.0)",
    )
    keyed = adapter.diff(branch, ["id"]).impact
    assert (keyed.rows_added, keyed.rows_deleted, keyed.rows_modified) == (1, 1, 2)
    assert keyed.rows_affected == 3 and keyed.key == ("id",)
    keyless = adapter.diff(branch).impact
    assert (keyless.rows_added, keyless.rows_deleted, keyless.rows_modified) == (3, 3, None)
    assert keyless.rows_affected == 3  # same blast radius either way


def test_a_non_unique_key_falls_back_to_whole_row_comparison(adapter):
    branch = adapter.create_branch("orders")
    adapter.apply_sql(branch, "DELETE FROM orders WHERE id = 1")
    impact = adapter.diff(branch, ["customer"]).impact  # customer repeats, so it is no key
    assert impact.key is None and impact.rows_modified is None and impact.rows_deleted == 1


# -- maintenance planning ------------------------------------------------------------------------


def test_rollback_plan_refuses_unknown_and_non_ancestor_snapshots(adapter, orders):
    from sqlguard.safety import RollbackToSnapshot

    with pytest.raises(ExecutionError, match="unknown snapshot id"):
        adapter.plan_maintenance(RollbackToSnapshot("orders", snapshot_id=12345))
    branch = adapter.create_branch("orders")
    adapter.apply_sql(branch, "DELETE FROM orders WHERE id = 1")
    head = orders.table("orders").refs()[branch.name].snapshot_id
    with pytest.raises(ExecutionError, match="not an ancestor"):
        adapter.plan_maintenance(RollbackToSnapshot("orders", snapshot_id=head))
    with pytest.raises(ExecutionError, match="exactly one"):
        adapter.plan_maintenance(RollbackToSnapshot("orders"))
