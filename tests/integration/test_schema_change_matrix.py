# ruff: noqa: F811  (the fixtures are imported from test_pending_matrix, then named as arguments)
"""Column drops and renames on real catalogs: a pending change is applied once, with the catalog's
own compare-and-swap on the schema and on main, and the table stays usable afterwards.

Each test runs once per catalog (Lakekeeper, the REST fixture, Polaris) with the PyIceberg-backed
adapter. The last block runs Trino: alone it cannot apply a schema change and says so; reading
next to the PyIceberg writer it sees the new columns.
"""

from __future__ import annotations

import pytest
from pyiceberg.table import Transaction

from sqlguard.core.catalog import LakehouseCatalog
from sqlguard.safety import ActionState, Guard, Policy, SqlAction, TaskScope
from sqlguard.safety.executor import DuckDBIcebergAdapter
from sqlguard.safety.executor.composite import CompositeAdapter
from sqlguard.safety.executor.trino_iceberg import TrinoIcebergAdapter
from sqlguard.safety.guard import Outcome
from sqlguard.safety.store import ALTER_KIND
from tests.integration.conftest import ROWS, main_id, order_ids
from tests.integration.test_pending_matrix import (  # noqa: F401  (fixtures and helpers)
    APPROVER,
    Clock,
    clock,
    decide,
    linux_identity,
    lk,
    make_guard,
    one_row,
    pguard,
    queue,
    scope,
    store_path,
    trino,
    verifier,
)

RENAME = "ALTER TABLE orders RENAME COLUMN customer TO buyer"
DROP = "ALTER TABLE orders DROP COLUMN amount"
A = ActionState


def columns(lake: LakehouseCatalog, table: str = "orders") -> list[str]:
    return [f.name for f in lake.table(table).schema().fields]


def rows(lake: LakehouseCatalog) -> dict[int, dict]:
    return {r["id"]: r for r in lake.read("orders").to_pylist()}


def other_writer_renames(lake: LakehouseCatalog, old: str = "amount", new: str = "total") -> None:
    with lake.table("orders").update_schema() as update:
        update.rename_column(old, new)


# -- pending rename: approve ---------------------------------------------------------------------


def test_a_pending_rename_is_applied_on_approval_without_moving_main(
    pguard: Guard, verifier, lake: LakehouseCatalog, scope: TaskScope
):
    base = main_id(lake)
    schema_before = lake.table("orders").schema().schema_id
    result = queue(pguard, scope, RENAME)
    row = pguard.store.get(result.action_id)

    assert row.kind == ALTER_KIND and row.state is A.PENDING and row.branch is None
    assert row.base_snapshot_id == base
    change = row.impact_data["schema_change"]
    assert change["rehearsed"] is False and change["renamed"] == [["customer", "buyer"]]
    assert change["schema_id"] == schema_before
    assert columns(lake) == ["id", "customer", "amount"] and main_id(lake) == base  # nothing yet

    done = decide(pguard, verifier, result)
    assert done.outcome is Outcome.PUBLISHED and done.published
    assert columns(lake) == ["id", "buyer", "amount"]
    assert main_id(lake) == base  # a schema change moves no snapshot
    assert lake.table("orders").schema().schema_id != schema_before
    assert pguard.store.get(result.action_id).state is A.PUBLISHED
    assert order_ids(lake) == list(range(1, ROWS + 1))


def test_the_table_is_readable_and_writable_after_an_approved_rename(
    pguard: Guard, make_guard, verifier, lake: LakehouseCatalog, scope: TaskScope
):
    assert decide(pguard, verifier, queue(pguard, scope, RENAME)).outcome is Outcome.PUBLISHED
    guard = make_guard()

    star = guard.submit(SqlAction("SELECT * FROM orders WHERE id = 3"), scope)
    assert star.outcome is Outcome.READ
    assert star.data.column_names == ["id", "buyer", "amount"] and star.data.num_rows == 1
    count = guard.submit(SqlAction("SELECT count(*) AS n FROM orders"), scope)
    assert count.data.column("n").to_pylist() == [ROWS]

    delete = guard.submit(SqlAction("DELETE FROM orders WHERE id = 1"), scope, lambda r: True)
    assert delete.outcome is Outcome.PUBLISHED, (delete.reasons, delete.error)
    insert = guard.submit(
        SqlAction("INSERT INTO orders (id, buyer, amount) VALUES (500, 'new', 9.5)"),
        scope,
        lambda r: True,
    )
    assert insert.outcome is Outcome.PUBLISHED, (insert.reasons, insert.error)
    update = guard.submit(
        SqlAction("UPDATE orders SET buyer = 'edited' WHERE id = 2"), scope, lambda r: True
    )
    assert update.outcome is Outcome.PUBLISHED, (update.reasons, update.error)

    now = rows(lake)
    assert 1 not in now and now[500]["buyer"] == "new" and now[2]["buyer"] == "edited"
    assert now[3]["buyer"] == "c2"  # the renamed column kept its data
    assert columns(lake) == ["id", "buyer", "amount"]


def test_a_write_that_waits_for_approval_after_a_rename_still_applies(
    pguard: Guard, verifier, lake: LakehouseCatalog, scope: TaskScope
):
    decide(pguard, verifier, queue(pguard, scope, RENAME))
    delete = queue(pguard, scope, "DELETE FROM orders WHERE id <= 15")
    assert pguard.diff_for(delete.action_id).rows_deleted == 15
    assert decide(pguard, verifier, delete).outcome is Outcome.PUBLISHED
    assert order_ids(lake) == list(range(16, ROWS + 1))


def test_a_drop_is_applied_and_the_table_stays_writable(
    pguard: Guard, make_guard, verifier, lake: LakehouseCatalog, scope: TaskScope
):
    assert decide(pguard, verifier, queue(pguard, scope, DROP)).outcome is Outcome.PUBLISHED
    assert columns(lake) == ["id", "customer"]
    guard = make_guard()
    assert guard.submit(
        SqlAction("SELECT * FROM orders WHERE id = 3"), scope
    ).data.column_names == [
        "id",
        "customer",
    ]
    for sql in (
        "DELETE FROM orders WHERE id = 1",
        "INSERT INTO orders (id, customer) VALUES (500, 'n')",
        "UPDATE orders SET customer = 'e' WHERE id = 2",
    ):
        result = guard.submit(SqlAction(sql), scope, lambda r: True)
        assert result.outcome is Outcome.PUBLISHED, (sql, result.reasons, result.error)
    assert 1 not in rows(lake) and rows(lake)[500]["customer"] == "n"


def test_a_rename_in_the_synchronous_path_is_applied_only_when_the_approver_says_yes(
    make_guard, lake: LakehouseCatalog, scope: TaskScope
):
    guard = make_guard()
    no = guard.submit(SqlAction(RENAME), scope, lambda r: False)
    assert no.outcome is Outcome.REJECTED and columns(lake) == ["id", "customer", "amount"]
    yes = guard.submit(SqlAction(RENAME), scope, lambda r: r.schema is not None)
    assert yes.outcome is Outcome.PUBLISHED and columns(lake) == ["id", "buyer", "amount"]


# -- pending drop: reject ------------------------------------------------------------------------


def test_a_rejected_drop_changes_nothing(
    pguard: Guard, verifier, lake: LakehouseCatalog, scope: TaskScope
):
    base, schema = main_id(lake), lake.table("orders").schema().schema_id
    result = queue(pguard, scope, DROP)
    done = decide(pguard, verifier, result, "reject")
    assert done.outcome is Outcome.REJECTED and not done.executed
    assert pguard.store.get(result.action_id).state is A.REJECTED
    assert columns(lake) == ["id", "customer", "amount"]
    assert main_id(lake) == base and lake.table("orders").schema().schema_id == schema
    assert order_ids(lake) == list(range(1, ROWS + 1))


# -- another writer ------------------------------------------------------------------------------


def test_a_schema_changed_by_another_writer_before_approval_makes_it_stale(
    pguard: Guard, verifier, lake: LakehouseCatalog, scope: TaskScope
):
    result = queue(pguard, scope, RENAME)
    other_writer_renames(lake)
    done = decide(pguard, verifier, result)
    assert done.outcome is Outcome.STALE
    assert any("columns differ" in r for r in done.reasons)
    assert pguard.store.get(result.action_id).state is A.STALE
    assert columns(lake) == ["id", "customer", "total"]  # only the other writer's change


def test_main_moved_by_another_writer_before_approval_makes_it_stale(
    pguard: Guard, verifier, lake: LakehouseCatalog, scope: TaskScope
):
    result = queue(pguard, scope, RENAME)
    lake.table("orders").append(one_row(500))
    assert decide(pguard, verifier, result).outcome is Outcome.STALE
    assert columns(lake) == ["id", "customer", "amount"]


@pytest.mark.parametrize("racer", ["schema", "main"])
def test_the_catalog_refuses_the_commit_when_a_writer_lands_between_the_check_and_the_commit(
    pguard: Guard, verifier, lake: LakehouseCatalog, scope: TaskScope, monkeypatch, racer
):
    """The Guard's own checks pass; the catalog's requirements (the schema id and main's
    snapshot, asserted in the commit) are what stop the change."""
    result = queue(pguard, scope, RENAME)
    original = Transaction.commit_transaction
    fired = []

    def race_then_commit(self):
        if not fired:
            fired.append(True)
            if racer == "schema":
                other_writer_renames(lake)
            else:
                lake.table("orders").append(one_row(500))
        return original(self)

    monkeypatch.setattr(Transaction, "commit_transaction", race_then_commit)
    done = decide(pguard, verifier, result)
    monkeypatch.undo()
    assert fired and done.outcome is Outcome.STALE, (done.outcome, done.reasons, done.error)
    assert pguard.store.get(result.action_id).state is A.STALE
    expected = ["id", "customer", "total"] if racer == "schema" else ["id", "customer", "amount"]
    assert columns(lake) == expected  # never the half-applied mix


def test_a_pending_write_goes_stale_when_a_column_it_was_measured_on_is_dropped(
    pguard: Guard, verifier, lake: LakehouseCatalog, scope: TaskScope
):
    update = pguard.submit(
        SqlAction("UPDATE orders SET amount = amount + 100 WHERE id <= 15"), scope
    )
    assert update.outcome is Outcome.AWAITING_APPROVAL, update.reasons
    assert decide(pguard, verifier, queue(pguard, scope, DROP)).outcome is (Outcome.PUBLISHED)
    done = decide(pguard, verifier, update)
    assert done.outcome is Outcome.STALE and any("columns changed" in r for r in done.reasons)
    assert set(lake.refs("orders")) == {"main"}  # the audit branch is gone


# -- plans the table must not take ---------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "ALTER TABLE orders RENAME COLUMN customer TO AMOUNT",
        "ALTER TABLE orders DROP COLUMN id, DROP COLUMN customer, DROP COLUMN amount",
        "ALTER TABLE orders ALTER COLUMN amount SET DATA TYPE DECIMAL(10, 2)",
    ],
)
def test_a_plan_the_table_must_not_take_is_denied_and_nothing_is_queued(
    pguard: Guard, lake: LakehouseCatalog, scope: TaskScope, sql
):
    result = pguard.submit(SqlAction(sql), scope, lambda r: True)
    assert result.outcome is Outcome.DENIED and not result.executed
    assert pguard.store.list_actions() == [] and columns(lake) == ["id", "customer", "amount"]


def test_a_column_renamed_by_another_writer_leaves_the_table_readable_and_writable(
    make_guard, lake: LakehouseCatalog, scope: TaskScope
):
    other_writer_renames(lake, "customer", "buyer")
    guard = make_guard()
    read = guard.submit(SqlAction("SELECT * FROM orders WHERE id = 3"), scope)
    assert read.outcome is Outcome.READ and read.data.column_names == ["id", "buyer", "amount"]
    delete = guard.submit(SqlAction("DELETE FROM orders WHERE id = 1"), scope, lambda r: True)
    assert delete.outcome is Outcome.PUBLISHED, (delete.reasons, delete.error)


def test_a_column_the_sort_order_or_an_older_partition_spec_used_cannot_be_dropped(
    make_guard, lake: LakehouseCatalog, scope: TaskScope
):
    from pyiceberg.transforms import IdentityTransform

    with lake.table("orders").transaction() as tx:
        tx.update_sort_order().asc("amount", IdentityTransform()).commit()
    with lake.table("orders").update_spec() as spec:
        spec.add_identity("customer")
    lake.table("orders").append(one_row(500))
    with lake.table("orders").update_spec() as spec:
        spec.remove_field("customer")
    guard = make_guard()
    for column, reason in (("amount", "sort order"), ("customer", "partition spec")):
        result = guard.submit(
            SqlAction(f"ALTER TABLE orders DROP COLUMN {column}"), scope, lambda r: True
        )
        assert result.outcome is Outcome.DENIED, (column, result.reasons, result.error)
        assert reason in " ".join(result.reasons)
    assert columns(lake) == ["id", "customer", "amount"]


# -- Trino ---------------------------------------------------------------------------------------


def test_trino_alone_denies_a_schema_change_and_names_itself(
    trino: TrinoIcebergAdapter, lk: LakehouseCatalog, tmp_path
):
    guard = Guard(lk, Policy(), audit_path=tmp_path / "audit.jsonl", adapter=trino)
    scope = TaskScope.of(namespaces=[lk.namespace])
    assert not trino.capabilities.schema_change
    result = guard.submit(SqlAction(RENAME), scope, lambda r: True)
    assert result.outcome is Outcome.DENIED and not result.executed
    assert any("trino cannot apply schema changes" in r for r in result.reasons)
    assert columns(lk) == ["id", "customer", "amount"]


def test_trino_reads_the_new_columns_after_the_pyiceberg_writer_applies_a_rename(
    lk: LakehouseCatalog, trino: TrinoIcebergAdapter, tmp_path, store_path, verifier, clock
):
    guard = Guard(
        lk,
        Policy(),
        audit_path=tmp_path / "audit.jsonl",
        adapter=CompositeAdapter(read=trino, write=DuckDBIcebergAdapter(lk)),
        store=store_path,
        proof_verifier=verifier,
        clock=clock,
    )
    assert guard.adapter.capabilities.schema_change  # the write engine decides
    scope = TaskScope.of(namespaces=[lk.namespace])
    result = queue(guard, scope, RENAME)
    assert columns(lk) == ["id", "customer", "amount"]
    done = decide(guard, verifier, result)
    assert done.outcome is Outcome.PUBLISHED, (done.reasons, done.error)
    assert columns(lk) == ["id", "buyer", "amount"]
    seen = trino.read("orders")
    assert seen.column_names == ["id", "buyer", "amount"] and seen.num_rows == ROWS
    read = guard.submit(SqlAction("SELECT count(*) AS n FROM orders"), scope)
    assert read.outcome is Outcome.READ and read.data.column("n").to_pylist() == [ROWS]
    delete = guard.submit(SqlAction("DELETE FROM orders WHERE id = 1"), scope, lambda r: True)
    assert delete.outcome is Outcome.PUBLISHED, (delete.reasons, delete.error)
    assert 1 not in order_ids(lk)
