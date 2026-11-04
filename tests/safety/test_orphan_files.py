from __future__ import annotations

import os
import shutil
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

import pyarrow as pa
import pytest

from sqlguard.safety.executor import OrphanFile, delete_orphans, find_orphans, referenced_paths

FUTURE = datetime.now(UTC) + timedelta(days=1)  # makes every file count as old enough
LONG_AGO = datetime(2000, 1, 1, tzinfo=UTC)


def location(table) -> Path:
    return Path(urlparse(table.location()).path)


def stray(table, name: str = "data/stray.parquet", age_days: float = 30) -> Path:
    path = location(table) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not referenced by anything")
    old = time.time() - age_days * 86400
    os.utime(path, (old, old))
    return path


def all_files(table) -> set[Path]:
    return {p for p in location(table).rglob("*") if p.is_file()}


def paths(orphans: list[OrphanFile]) -> set[Path]:
    return {Path(urlparse(o.location).path) for o in orphans}


def one_row(order_id: int) -> pa.Table:
    return pa.table(
        {
            "id": pa.array([order_id], pa.int64()),
            "customer": ["x"],
            "amount": pa.array([1.0]),
        }
    )


@pytest.fixture
def table(orders):
    return orders.table("orders")


def test_a_healthy_table_has_no_orphans_even_with_a_generous_cutoff(orders):
    orders.table("orders").append(one_row(100))
    assert find_orphans(orders.table("orders"), FUTURE) == []


def test_everything_the_metadata_points_to_is_referenced(orders):
    orders.table("orders").append(one_row(100))
    iceberg = orders.table("orders")
    assert {str(p) for p in all_files(iceberg)} <= referenced_paths(iceberg)


def test_stray_file_is_found_by_the_dry_run_and_not_removed_by_it(table):
    path = stray(table)
    found = find_orphans(table, FUTURE)
    assert paths(found) == {path}
    assert found[0].size == len(b"not referenced by anything")
    assert path.exists()


def test_recent_files_are_protected_by_older_than(table):
    young = stray(table, "data/in-flight.parquet", age_days=0)
    old = stray(table, "data/old.parquet", age_days=10)
    found = paths(find_orphans(table, datetime.now(UTC) - timedelta(days=3)))
    assert found == {old} and young not in found


def test_naive_cutoff_is_treated_as_utc(table):
    stray(table)
    naive = datetime.now(UTC).replace(tzinfo=None) + timedelta(days=1)
    assert len(find_orphans(table, naive)) == 1


def test_delete_removes_only_the_orphans(table):
    path = stray(table)
    before = all_files(table)
    removed = delete_orphans(table, find_orphans(table, FUTURE), FUTURE)
    assert paths(removed) == {path}
    assert all_files(table) == before - {path}


def test_referenced_files_are_never_deleted_even_if_asked(orders):
    iceberg = orders.table("orders")
    data_file = next(p for p in all_files(iceberg) if p.suffix == ".parquet")
    forged = OrphanFile(f"file://{data_file}", 1, LONG_AGO)
    assert delete_orphans(iceberg, [forged], FUTURE) == []
    assert data_file.exists()
    assert orders.read("orders").num_rows == 20


def test_files_outside_the_table_directory_are_never_touched(table, tmp_path):
    sibling = location(table).parent / "other_table" / "data" / "x.parquet"
    sibling.parent.mkdir(parents=True)
    sibling.write_bytes(b"belongs to someone else")
    outside = tmp_path / "elsewhere.parquet"
    outside.write_bytes(b"outside the warehouse")
    prefix_trick = Path(str(location(table)) + "_evil") / "x.parquet"
    prefix_trick.parent.mkdir(parents=True)
    prefix_trick.write_bytes(b"shares the table path as a string prefix")

    assert find_orphans(table, FUTURE) == []
    forged = [OrphanFile(f"file://{p}", 1, LONG_AGO) for p in (sibling, outside, prefix_trick)]
    assert delete_orphans(table, forged, FUTURE) == []
    assert sibling.exists() and outside.exists() and prefix_trick.exists()


def test_symlinks_are_ignored(table, tmp_path):
    real_file = tmp_path / "real.parquet"
    real_file.write_bytes(b"outside")
    link = location(table) / "data" / "link.parquet"
    link.symlink_to(real_file)
    assert find_orphans(table, FUTURE) == []
    assert delete_orphans(table, [OrphanFile(f"file://{link}", 1, LONG_AGO)], FUTURE) == []
    assert real_file.exists() and link.is_symlink()


def test_data_of_every_remaining_snapshot_is_kept_including_branches_and_tags(orders):
    iceberg = orders.table("orders")
    base = iceberg.current_snapshot().snapshot_id
    iceberg.manage_snapshots().create_branch(base, "audit").create_tag(base, "v1").commit()
    orders.table("orders").overwrite(one_row(1), branch="audit")  # only the branch has this data
    orders.table("orders").overwrite(one_row(2))  # main moves on; the tag pins the original
    iceberg = orders.table("orders")

    assert find_orphans(iceberg, FUTURE) == []
    assert orders.read("orders", iceberg.refs()["v1"].snapshot_id).num_rows == 20
    assert orders.read("orders", iceberg.refs()["audit"].snapshot_id).column("id").to_pylist() == [
        1
    ]


def test_files_of_unreferenced_snapshots_stay_until_those_snapshots_expire(orders):
    iceberg = orders.table("orders")
    base = iceberg.current_snapshot().snapshot_id
    iceberg.manage_snapshots().create_branch(base, "dropped").commit()
    orders.table("orders").overwrite(one_row(1), branch="dropped")
    orders.table("orders").manage_snapshots().remove_branch("dropped").commit()
    iceberg = orders.table("orders")
    assert find_orphans(iceberg, FUTURE) == []  # snapshots still list those files

    pinned = {r.snapshot_id for r in iceberg.refs().values()}
    leftovers = [s.snapshot_id for s in iceberg.snapshots() if s.snapshot_id not in pinned]
    assert leftovers
    iceberg.maintenance.expire_snapshots().by_ids(leftovers).commit()
    assert find_orphans(orders.table("orders"), FUTURE)  # now the branch's data is unreferenced


def test_expire_then_delete_orphans_loses_old_data_but_keeps_current_state(orders):
    first = orders.table("orders").current_snapshot().snapshot_id
    orders.table("orders").overwrite(one_row(500))
    orders.table("orders").overwrite(one_row(501))
    iceberg = orders.table("orders")
    head = iceberg.current_snapshot().snapshot_id

    assert find_orphans(iceberg, FUTURE) == []  # history alone keeps every file alive
    iceberg.maintenance.expire_snapshots().older_than(FUTURE).commit()
    iceberg = orders.table("orders")
    assert [s.snapshot_id for s in iceberg.snapshots()] == [head]
    assert orders.read("orders", head).column("id").to_pylist() == [501]

    orphans = find_orphans(iceberg, FUTURE)
    assert orphans  # expiry alone removed no data; these files are now unreferenced
    assert all(p.exists() for p in paths(orphans))
    removed = delete_orphans(iceberg, orphans, FUTURE)
    assert len(removed) == len(orphans) and not any(p.exists() for p in paths(removed))
    assert find_orphans(iceberg, FUTURE) == []
    assert orders.read("orders").column("id").to_pylist() == [501]
    with pytest.raises(Exception):  # noqa: B017, PT011  # the original data is physically gone
        orders.read("orders", first)


def test_unreadable_metadata_aborts_instead_of_guessing(table):
    """If the references cannot be computed, nothing may be called an orphan."""
    stray(table)
    shutil.rmtree(location(table) / "metadata")
    with pytest.raises(OSError):
        find_orphans(table, FUTURE)
    with pytest.raises(OSError):
        delete_orphans(table, [OrphanFile("file:///x", 1, LONG_AGO)], FUTURE)
