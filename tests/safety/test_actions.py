from __future__ import annotations

from datetime import datetime

import pytest

from sqlguard.safety.actions import (
    ExpireSnapshots,
    FastForward,
    RemoveOrphanFiles,
    RollbackToSnapshot,
    UnsupportedCall,
    parse_spark_call,
    qualify_table_name,
)


def test_expire_snapshots_named_arguments():
    action = parse_spark_call(
        "cat.system.expire_snapshots(table => 'db.orders', "
        "older_than => TIMESTAMP '2026-01-02 03:04:05', retain_last => 4)"
    )
    assert action == ExpireSnapshots(
        "db.orders", older_than=datetime(2026, 1, 2, 3, 4, 5), retain_last=4
    )


def test_expire_snapshots_positional_and_snapshot_ids():
    action = parse_spark_call(
        "system.expire_snapshots('db.orders', TIMESTAMP '2026-01-01 00:00:00', 2, 4, true, "
        "array(10, 11))"
    )
    assert isinstance(action, ExpireSnapshots)
    assert action.retain_last == 2
    assert action.snapshot_ids == (10, 11)


def test_remove_orphan_files_and_rollback_and_fast_forward():
    orphans = parse_spark_call(
        "system.remove_orphan_files('db.t', TIMESTAMP '2026-01-01 00:00:00')"
    )
    assert isinstance(orphans, RemoveOrphanFiles) and orphans.older_than is not None
    assert parse_spark_call("system.rollback_to_snapshot('db.t', 5)") == RollbackToSnapshot(
        "db.t", snapshot_id=5
    )
    assert parse_spark_call("system.set_current_snapshot(table => 'db.t', ref => 'b')") == (
        RollbackToSnapshot("db.t", ref="b")
    )
    assert parse_spark_call("system.fast_forward('db.t', 'main', 'audit')") == FastForward(
        "db.t", branch="main", to="audit"
    )


def test_comments_and_quotes_do_not_confuse_the_parser():
    action = parse_spark_call(
        "system.rollback_to_snapshot(/* ) */ 'db.we,ird', -- trailing )\n 7) /* end */ ;"
    )
    assert action == RollbackToSnapshot("db.we,ird", snapshot_id=7)


def test_non_literal_older_than_is_none_not_a_guess():
    action = parse_spark_call("system.expire_snapshots(table => 'db.t', older_than => now())")
    assert isinstance(action, ExpireSnapshots) and action.older_than is None


@pytest.mark.parametrize(
    "text",
    [
        "",
        "system.expire_snapshots",
        "system.expire_snapshots('db.t'",
        "system.expire_snapshots('db.t'))",
        "expire_snapshots('db.t')",
        "other.expire_snapshots('db.t')",
        "system.rewrite_data_files('db.t')",
        "system.expire_snapshots(table => 'db.t', bogus => 1)",
        "system.expire_snapshots('db.t', retain_last => 1)",
        "system.expire_snapshots(table => 'db.t', table => 'db.u')",
        "system.expire_snapshots(table => concat('a', 'b'))",
        "system.expire_snapshots(table => 'db.t', retain_last => 'x')",
        "system.expire_snapshots(table => 'db.t', snapshot_ids => array(1, x))",
        "system.expire_snapshots('db.t', null, 1, 1, true, array(1), true, 99)",
        "system.rollback_to_snapshot('db.t')",
        "system.set_current_snapshot('db.t', 1, 'ref')",
        "system.fast_forward('db.t', 'main')",
        "system.rollback_to_snapshot('db.t', 1,, 2)",
        "system.rollback_to_snapshot('db.t', 1) extra",
        "system.rollback_to_snapshot('db.t', 1 /* open",
    ],
)
def test_unverifiable_calls_are_rejected(text):
    with pytest.raises(UnsupportedCall):
        parse_spark_call(text)


def test_qualify_table_name_lowercases_and_adds_namespace():
    assert qualify_table_name("Orders", "db") == "db.orders"
    assert qualify_table_name(" `Other.T` ", "db") == "other.t"
