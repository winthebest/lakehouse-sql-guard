"""Column drops and renames: planned, never rehearsed, applied only after approval and only if
main and the schema are still what the reviewer saw."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pyarrow as pa
import pytest
from pyiceberg.table import Transaction

from sqlguard.core.catalog import LakehouseCatalog
from sqlguard.core.findings import Decision
from sqlguard.safety import (
    ActionState,
    ColumnChange,
    Guard,
    Policy,
    ReversibilityClass,
    SqlAction,
    classify,
)
from sqlguard.safety.executor import (
    BranchDiverged,
    Capabilities,
    CompositeAdapter,
    DuckDBIcebergAdapter,
    ExecutionError,
    PublishStateUnknown,
    SchemaChangeRefused,
    SparkIcebergAdapter,
    TrinoIcebergAdapter,
    UnsupportedAction,
)
from sqlguard.safety.guard import Outcome
from sqlguard.safety.store import ALTER_KIND
from tests.safety.conftest import ROWS, main_ids, order_ids
from tests.safety.test_pending_actions import Clock, HmacVerifier, decide

RENAME = "ALTER TABLE orders RENAME COLUMN customer TO buyer"
DROP = "ALTER TABLE orders DROP COLUMN amount"


def names(catalog: LakehouseCatalog, table: str = "orders") -> list[str]:
    return [f.name for f in catalog.table(table).schema().fields]


@pytest.fixture(autouse=True)
def linux_identity(monkeypatch):
    """Give the process a boot id and PID namespace, as on Linux, so a dead pid can be proven."""
    import sqlguard.safety.guard as guard_module

    monkeypatch.setattr(guard_module, "_boot_id", lambda: "boot-1")
    monkeypatch.setattr(guard_module, "_pid_namespace", lambda: "pid:[1]")


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def verifier() -> HmacVerifier:
    return HmacVerifier()


@pytest.fixture
def pguard(make_guard, tmp_path: Path, verifier, clock) -> Guard:
    return make_guard(store=tmp_path / "state" / "actions.db", proof_verifier=verifier, clock=clock)


def queue(guard: Guard, scope, sql: str = RENAME):
    result = guard.submit(SqlAction(sql), scope)
    assert result.outcome is Outcome.AWAITING_APPROVAL and result.action_id, result.reasons
    return result


# -- classification ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        (RENAME, (ColumnChange("rename", "customer", "buyer"),)),
        (DROP, (ColumnChange("drop", "amount"),)),
        (
            "ALTER TABLE orders DROP COLUMN amount, DROP COLUMN customer",
            (ColumnChange("drop", "amount"), ColumnChange("drop", "customer")),
        ),
    ],
)
def test_a_column_drop_or_rename_is_recognised(sql, expected):
    statement = classify(SqlAction(sql)).statements[0]
    assert statement.klass is ReversibilityClass.SCHEMA_BREAKING
    assert statement.schema_change == expected


@pytest.mark.parametrize(
    "sql",
    [
        "ALTER TABLE orders ADD COLUMN note VARCHAR",
        "ALTER TABLE orders ALTER COLUMN amount SET DATA TYPE DECIMAL(10, 2)",
        "ALTER TABLE orders RENAME TO orders_old",
        "ALTER TABLE orders DROP COLUMN IF EXISTS amount",
        "ALTER TABLE orders DROP COLUMN o.amount",
        "ALTER TABLE orders RENAME COLUMN customer TO buyer, DROP COLUMN amount",
        "DELETE FROM orders WHERE id = 1",
    ],
)
def test_every_other_statement_has_no_schema_change(sql):
    assert classify(SqlAction(sql)).statements[0].schema_change is None


# -- the PyIceberg executor --------------------------------------------------------------------


@pytest.fixture
def adapter(orders: LakehouseCatalog) -> DuckDBIcebergAdapter:
    return DuckDBIcebergAdapter(orders)


def test_planning_shows_old_and_new_columns_and_changes_nothing(adapter, orders):
    snapshot = main_ids(orders)
    plan = adapter.plan_schema_change(
        "orders", [ColumnChange("rename", "customer", "buyer"), ColumnChange("drop", "amount")]
    )
    assert [c.name for c in plan.before] == ["id", "customer", "amount"]
    assert [c.name for c in plan.after] == ["id", "buyer"]
    assert plan.renamed == (("customer", "buyer"),) and plan.removed == ("amount",)
    assert plan.base_snapshot_id == snapshot[-1]
    assert plan.to_dict()["rehearsed"] is False
    assert names(orders) == ["id", "customer", "amount"] and main_ids(orders) == snapshot


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (ColumnChange("drop", "nope"), "nope"),
        (ColumnChange("rename", "nope", "x"), "nope"),
        (ColumnChange("rename", "customer", "amount"), "amount"),
    ],
)
def test_a_change_the_table_cannot_take_is_refused_when_planned(adapter, change, message):
    with pytest.raises(ExecutionError, match=message):
        adapter.plan_schema_change("orders", [change])


def test_the_partition_source_column_cannot_be_dropped(tmp_path):
    from pyiceberg.transforms import IdentityTransform

    lake = LakehouseCatalog.local(tmp_path / "lake")
    table = lake.create_table("orders", pa.table({"id": [1], "region": ["n"]}))
    with table.update_spec() as spec:
        spec.add_field("region", IdentityTransform(), "region")
    adapter = DuckDBIcebergAdapter(lake)
    with pytest.raises(ExecutionError, match="partition spec"):
        adapter.plan_schema_change("orders", [ColumnChange("drop", "region")])


def test_applying_changes_the_schema_and_leaves_main_on_the_same_snapshot(adapter, orders):
    snapshot = main_ids(orders)
    plan = adapter.plan_schema_change("orders", [ColumnChange("rename", "customer", "buyer")])
    adapter.apply_schema_change(plan)
    assert names(orders) == ["id", "buyer", "amount"]
    assert main_ids(orders) == snapshot
    assert order_ids(orders) == list(range(1, ROWS + 1))


def test_a_rename_keeps_the_data_of_the_column(adapter, orders):
    adapter.apply_schema_change(
        adapter.plan_schema_change("orders", [ColumnChange("rename", "customer", "buyer")])
    )
    assert orders.read("orders").column("buyer").to_pylist()[:2] == ["c0", "c1"]


def test_a_plan_is_refused_after_main_moved(adapter, orders):
    plan = adapter.plan_schema_change("orders", [ColumnChange("drop", "amount")])
    orders.table("orders").append(
        pa.table({"id": [99], "customer": ["z"], "amount": pa.array([1.0])})
    )
    with pytest.raises(BranchDiverged):
        adapter.apply_schema_change(plan)
    assert names(orders) == ["id", "customer", "amount"]


def test_a_plan_is_refused_after_the_schema_moved(adapter, orders):
    plan = adapter.plan_schema_change("orders", [ColumnChange("drop", "amount")])
    with orders.table("orders").update_schema() as update:
        update.rename_column("customer", "buyer")
    with pytest.raises(BranchDiverged):
        adapter.apply_schema_change(plan)
    assert names(orders) == ["id", "buyer", "amount"]


@pytest.mark.parametrize("racer", ["append", "rename"])
def test_the_catalog_refuses_a_commit_that_loses_a_race(adapter, orders, monkeypatch, racer):
    """A writer lands between our check and our commit: the catalog's own assertions stop us."""
    plan = adapter.plan_schema_change("orders", [ColumnChange("drop", "amount")])
    original = Transaction.commit_transaction
    fired = []

    def race_then_commit(self):
        if not fired:
            fired.append(True)
            if racer == "append":
                orders.table("orders").append(
                    pa.table({"id": [99], "customer": ["z"], "amount": pa.array([1.0])})
                )
            else:
                with orders.table("orders").update_schema() as update:
                    update.rename_column("customer", "buyer")
        return original(self)

    monkeypatch.setattr(Transaction, "commit_transaction", race_then_commit)
    with pytest.raises(BranchDiverged):
        adapter.apply_schema_change(plan)
    assert "amount" in names(orders)


def test_a_commit_of_unknown_outcome_that_changed_nothing_is_not_called_failed(
    adapter, orders, monkeypatch
):
    plan = adapter.plan_schema_change("orders", [ColumnChange("drop", "amount")])

    def lost(self):
        raise TimeoutError("gateway timeout")

    monkeypatch.setattr(Transaction, "commit_transaction", lost)
    monkeypatch.setattr(DuckDBIcebergAdapter, "_READBACK_DELAY_SECONDS", 0)
    with pytest.raises(PublishStateUnknown):
        adapter.apply_schema_change(plan)


def test_a_commit_of_unknown_outcome_that_landed_counts_as_applied(adapter, orders, monkeypatch):
    plan = adapter.plan_schema_change("orders", [ColumnChange("drop", "amount")])
    original = Transaction.commit_transaction

    def lands_then_times_out(self):
        original(self)
        raise TimeoutError("response lost")

    monkeypatch.setattr(Transaction, "commit_transaction", lands_then_times_out)
    adapter.apply_schema_change(plan)
    assert "amount" not in names(orders)


def test_only_the_pyiceberg_executor_applies_schema_changes():
    assert DuckDBIcebergAdapter.capabilities.schema_change
    assert not SparkIcebergAdapter.capabilities.schema_change
    assert not TrinoIcebergAdapter.capabilities.schema_change
    for engine in (SparkIcebergAdapter, TrinoIcebergAdapter):
        with pytest.raises(UnsupportedAction):
            engine.plan_schema_change(None, "orders", [])  # type: ignore[arg-type]
        with pytest.raises(UnsupportedAction):
            engine.apply_schema_change(None, None)  # type: ignore[arg-type]


def test_a_composite_takes_schema_changes_from_its_write_engine(orders):
    class Reader:
        engine = "reader"
        capabilities = Capabilities.full()

    write = DuckDBIcebergAdapter(orders)
    assert CompositeAdapter(Reader(), write).capabilities.schema_change
    no_write = dataclasses.replace(write.capabilities, schema_change=False)

    class Other(DuckDBIcebergAdapter):
        capabilities = no_write

    assert not CompositeAdapter(Reader(), Other(orders)).capabilities.schema_change


# -- the Guard: with an approver ---------------------------------------------------------------


def test_an_approved_rename_is_applied(make_guard, orders, scope):
    asked = []

    def approve(request) -> bool:
        asked.append(request)
        return True

    result = make_guard().submit(SqlAction(RENAME), scope, approve)
    assert result.outcome is Outcome.PUBLISHED and result.published and result.executed
    assert names(orders) == ["id", "buyer", "amount"]
    [request] = asked
    assert request.schema is not None and request.schema.renamed == (("customer", "buyer"),)
    assert any("not rehearsed" in r or "cannot be rehearsed" in r for r in request.decision.reasons)
    assert result.schema is not None and result.to_dict()["schema_change"]["rehearsed"] is False


def test_a_rejected_change_is_not_applied(make_guard, orders, scope):
    result = make_guard().submit(SqlAction(DROP), scope, lambda request: False)
    assert result.outcome is Outcome.REJECTED and not result.executed
    assert names(orders) == ["id", "customer", "amount"]


def test_without_an_approver_nothing_is_applied(make_guard, orders, scope):
    result = make_guard().submit(SqlAction(DROP), scope)
    assert result.outcome is Outcome.AWAITING_APPROVAL and result.action_id is None
    assert names(orders) == ["id", "customer", "amount"]


def test_a_policy_that_allows_schema_changes_still_asks(make_guard, orders, scope):
    base = Policy()
    allowing = dataclasses.replace(
        base,
        class_decisions={
            **base.class_decisions,
            ReversibilityClass.SCHEMA_BREAKING: Decision.ALLOW_VIA_BRANCH,
        },
    )
    result = make_guard(allowing).submit(SqlAction(DROP), scope)
    assert result.outcome is Outcome.AWAITING_APPROVAL and result.decision is Decision.ASK
    assert names(orders) == ["id", "customer", "amount"]


def test_a_policy_that_denies_schema_changes_still_denies(make_guard, orders, scope):
    base = Policy()
    denying = dataclasses.replace(
        base,
        class_decisions={**base.class_decisions, ReversibilityClass.SCHEMA_BREAKING: Decision.DENY},
    )
    result = make_guard(denying).submit(SqlAction(DROP), scope, lambda request: True)
    assert result.outcome is Outcome.DENIED
    assert names(orders) == ["id", "customer", "amount"]


def test_outside_the_task_scope_nothing_is_planned(make_guard, orders, scope):
    result = make_guard().submit(
        SqlAction("ALTER TABLE prod.orders DROP COLUMN amount"), scope, lambda r: True
    )
    assert result.outcome is Outcome.DENIED and result.schema is None


def test_a_missing_column_is_an_execution_failure(make_guard, orders, scope):
    result = make_guard().submit(
        SqlAction("ALTER TABLE orders DROP COLUMN nope"), scope, lambda r: True
    )
    assert result.outcome is Outcome.EXECUTION_FAILED and "nope" in (result.error or "")


def test_a_form_the_executor_cannot_apply_is_a_clear_denial(make_guard, orders, scope):
    result = make_guard().submit(
        SqlAction("ALTER TABLE orders ALTER COLUMN amount SET DATA TYPE DECIMAL(10, 2)"),
        scope,
        lambda r: True,
    )
    assert result.outcome is Outcome.DENIED and not result.executed
    assert any("DROP COLUMN and RENAME COLUMN" in r for r in result.reasons)


def test_a_mixed_alter_in_a_batch_is_unsupported_with_a_plain_reason(make_guard, orders, scope):
    result = make_guard().submit(
        SqlAction(f"{RENAME}; DELETE FROM orders WHERE id = 1"), scope, lambda r: True
    )
    assert result.outcome is Outcome.UNSUPPORTED
    assert any("only statement of a call" in r for r in result.reasons)
    assert names(orders) == ["id", "customer", "amount"]


def test_an_engine_that_cannot_alter_gets_a_denial_naming_it(orders, audit_path, scope):
    class NoAlter(DuckDBIcebergAdapter):
        capabilities = dataclasses.replace(DuckDBIcebergAdapter.capabilities, schema_change=False)
        engine = "no-alter"

    guard = Guard(orders, audit_path=audit_path, adapter=NoAlter(orders))
    result = guard.submit(SqlAction(DROP), scope, lambda r: True)
    assert result.outcome is Outcome.DENIED and not result.executed
    assert any("no-alter cannot apply schema changes" in r for r in result.reasons)
    assert names(orders) == ["id", "customer", "amount"]


def test_an_added_column_keeps_reporting_unsupported(make_guard, orders, scope):
    result = make_guard().submit(
        SqlAction("ALTER TABLE orders ADD COLUMN note VARCHAR"), scope, lambda r: True
    )
    assert result.outcome is Outcome.UNSUPPORTED and not result.executed


def test_the_audit_log_records_the_schema_change(make_guard, orders, scope, audit):
    make_guard().submit(SqlAction(RENAME), scope, lambda r: True)
    completed = [r for r in audit.read() if r.get("phase") == "completed"][-1]
    assert completed["schema_change"]["renamed"] == [["customer", "buyer"]]
    assert completed["outcome"] == "published"


# -- the Guard: pending approval ---------------------------------------------------------------


def test_a_column_change_waits_for_a_person_and_changes_nothing(pguard, orders, scope):
    snapshots = main_ids(orders)
    result = queue(pguard, scope)
    record = pguard.store.get(result.action_id)
    assert record.kind == ALTER_KIND and record.state is ActionState.PENDING
    assert record.branch is None and record.head_snapshot_id is None
    assert record.base_snapshot_id == snapshots[-1]
    change = record.impact_data["schema_change"]
    assert change["rehearsed"] is False and change["renamed"] == [["customer", "buyer"]]
    assert [c["name"] for c in change["columns_before"]] == ["id", "customer", "amount"]
    assert [c["name"] for c in change["columns_after"]] == ["id", "buyer", "amount"]
    assert any("cannot be rehearsed" in r for r in record.reasons)
    assert names(orders) == ["id", "customer", "amount"] and main_ids(orders) == snapshots
    assert set(orders.refs("orders")) == {"main"}  # no audit branch was made


def test_approval_applies_it_on_the_reviewed_snapshot(pguard, orders, scope, verifier):
    snapshots = main_ids(orders)
    result = queue(pguard, scope)
    decided = decide(pguard, verifier, result)
    assert decided.outcome is Outcome.PUBLISHED and decided.published
    assert names(orders) == ["id", "buyer", "amount"]
    assert main_ids(orders) == snapshots
    assert pguard.store.get(result.action_id).state is ActionState.PUBLISHED
    assert order_ids(orders) == list(range(1, ROWS + 1))


def test_a_moved_main_makes_the_approval_stale_and_applies_nothing(pguard, orders, scope, verifier):
    result = queue(pguard, scope)
    orders.table("orders").append(
        pa.table({"id": [99], "customer": ["z"], "amount": pa.array([1.0])})
    )
    decided = decide(pguard, verifier, result)
    assert decided.outcome is Outcome.STALE
    assert any("main moved" in r for r in decided.reasons)
    assert names(orders) == ["id", "customer", "amount"]
    assert pguard.store.get(result.action_id).state is ActionState.STALE


def test_a_changed_schema_makes_the_approval_stale(pguard, orders, scope, verifier):
    result = queue(pguard, scope, DROP)
    with orders.table("orders").update_schema() as update:
        update.rename_column("customer", "buyer")
    decided = decide(pguard, verifier, result)
    assert decided.outcome is Outcome.STALE
    assert names(orders) == ["id", "buyer", "amount"]


def test_losing_the_race_at_commit_time_is_stale(pguard, orders, scope, verifier, monkeypatch):
    result = queue(pguard, scope, DROP)
    original = Transaction.commit_transaction
    fired = []

    def race_then_commit(self):
        if not fired:
            fired.append(True)
            orders.table("orders").append(
                pa.table({"id": [99], "customer": ["z"], "amount": pa.array([1.0])})
            )
        return original(self)

    monkeypatch.setattr(Transaction, "commit_transaction", race_then_commit)
    decided = decide(pguard, verifier, result)
    assert decided.outcome is Outcome.STALE and "amount" in names(orders)
    assert pguard.store.get(result.action_id).state is ActionState.STALE


def test_a_rejection_applies_nothing(pguard, orders, scope, verifier):
    result = queue(pguard, scope)
    decided = decide(pguard, verifier, result, "reject")
    assert decided.outcome is Outcome.REJECTED
    assert names(orders) == ["id", "customer", "amount"]
    assert pguard.store.get(result.action_id).state is ActionState.REJECTED


def test_a_changed_policy_makes_the_approval_stale(
    pguard, make_guard, orders, scope, verifier, tmp_path
):
    result = queue(pguard, scope)
    stricter = dataclasses.replace(Policy(), require_table_key=True)
    other = make_guard(stricter, store=pguard.store, proof_verifier=verifier, clock=pguard._clock)
    decided = decide(other, verifier, result)
    assert decided.outcome is Outcome.STALE
    assert names(orders) == ["id", "customer", "amount"]


def test_the_hash_covers_the_columns_that_were_reviewed(pguard, scope):
    result = queue(pguard, scope)
    record = pguard.store.get(result.action_id)
    forged = dataclasses.replace(record, diff_digest="0" * 64)
    assert forged.computed_hash() != record.action_hash


def test_a_second_identical_request_is_a_separate_action(pguard, scope):
    first, second = queue(pguard, scope), queue(pguard, scope)
    assert first.action_id != second.action_id


def test_a_crashed_publish_that_landed_is_settled_as_published(
    pguard, orders, scope, verifier, clock, monkeypatch
):
    result = queue(pguard, scope)

    real = DuckDBIcebergAdapter.apply_schema_change

    def land_then_die(self, plan):
        real(self, plan)
        raise KeyboardInterrupt

    monkeypatch.setattr(DuckDBIcebergAdapter, "apply_schema_change", land_then_die)
    with pytest.raises(KeyboardInterrupt):
        decide(pguard, verifier, result)
    assert pguard.store.get(result.action_id).state is ActionState.PUBLISHING
    clock.advance(minutes=6)
    [settled] = pguard.reconcile()
    assert settled.state is ActionState.PUBLISHED


def test_a_crashed_publish_that_did_not_land_is_failed(
    pguard, orders, scope, verifier, clock, monkeypatch
):
    result = queue(pguard, scope)

    def die(self, plan):
        raise KeyboardInterrupt

    monkeypatch.setattr(DuckDBIcebergAdapter, "apply_schema_change", die)
    with pytest.raises(KeyboardInterrupt):
        decide(pguard, verifier, result)
    clock.advance(minutes=6)
    [settled] = pguard.reconcile()
    assert settled.state is ActionState.FAILED
    assert names(orders) == ["id", "customer", "amount"]


def test_the_tool_backend_tells_the_agent_it_waits_and_that_nothing_was_rehearsed(
    pguard, orders, scope
):
    from sqlguard.safety.tools import STATUS_TOOL, GuardedBackend

    backend = GuardedBackend(pguard, scope)
    result = backend.call("run_sql", {"sql": RENAME})
    assert result.status == "pending_approval" and result.klass == "schema_breaking"
    assert "cannot be rehearsed" in result.payload["message"]
    assert "rehearsed and is waiting" not in result.payload["message"]
    assert result.payload["schema_change"]["renamed"] == [["customer", "buyer"]]
    assert "not rehearsed" in result.payload["summary"]
    status = backend.call(STATUS_TOOL, {"action_id": result.payload["action_id"]})
    assert status.payload["state"] == "pending" and "not rehearsed" in status.payload["summary"]


def test_a_protected_table_cannot_have_its_columns_changed(make_guard, orders, scope):
    guard = make_guard(Policy.from_dict({"protected_tables": ["orders"]}))
    result = guard.submit(SqlAction(DROP), scope, lambda r: True)
    assert result.outcome is Outcome.DENIED and names(orders) == ["id", "customer", "amount"]


def test_a_change_nobody_decides_expires_without_touching_the_table(pguard, orders, scope, clock):
    result = queue(pguard, scope)
    clock.advance(hours=25)
    assert pguard.expire_pending() == [result.action_id]
    assert pguard.store.get(result.action_id).state is ActionState.EXPIRED
    assert names(orders) == ["id", "customer", "amount"]


def test_a_version_2_store_is_stamped_for_the_new_kind_and_keeps_its_rows(pguard, scope, tmp_path):
    import sqlite3

    from sqlguard.safety import ActionStore
    from sqlguard.safety.store import SCHEMA_VERSION

    result = queue(pguard, scope)
    path = pguard.store.path
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA user_version = 2")
    conn.commit()
    conn.close()
    reopened = ActionStore(path)
    assert reopened.get(result.action_id).kind == ALTER_KIND
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 3
    conn.close()


# -- writing after a schema change -------------------------------------------------------------

WRITES = [
    "DELETE FROM orders WHERE id = 1",
    "INSERT INTO orders (id) VALUES (500)",
    "UPDATE orders SET id = 600 WHERE id = 2",
]


def rows_of(orders) -> dict[int, dict]:
    return {r["id"]: r for r in orders.read("orders").to_pylist()}


@pytest.mark.parametrize("write", WRITES)
@pytest.mark.parametrize("alter", [DROP, RENAME], ids=["drop", "rename"])
def test_the_table_can_still_be_written_after_the_guard_changed_its_columns(
    make_guard, orders, scope, alter, write
):
    assert make_guard().submit(SqlAction(alter), scope, lambda r: True).outcome is Outcome.PUBLISHED
    result = make_guard().submit(SqlAction(write), scope, lambda r: True)
    assert result.outcome is Outcome.PUBLISHED, (result.reasons, result.error)
    expected = set(names(orders))
    assert expected == ({"id", "customer"} if alter == DROP else {"id", "buyer", "amount"})
    assert set(orders.read("orders").column_names) == expected


def test_a_write_after_a_rename_keeps_the_renamed_column_data(make_guard, orders, scope):
    make_guard().submit(SqlAction(RENAME), scope, lambda r: True)
    make_guard().submit(
        SqlAction("UPDATE orders SET buyer = 'z' WHERE id = 3"), scope, lambda r: True
    )
    assert rows_of(orders)[3]["buyer"] == "z" and rows_of(orders)[4]["buyer"] == "c3"


@pytest.mark.parametrize("alter", [DROP, RENAME], ids=["drop", "rename"])
def test_a_pending_write_after_an_approved_change_is_queued_and_applies(
    pguard, orders, scope, verifier, alter
):
    decide(pguard, verifier, queue(pguard, scope, alter), nonce="a")
    delete = pguard.submit(SqlAction("DELETE FROM orders WHERE id <= 15"), scope)
    assert delete.outcome is Outcome.AWAITING_APPROVAL, (delete.reasons, delete.error)
    assert decide(pguard, verifier, delete, nonce="b").outcome is Outcome.PUBLISHED
    assert sorted(rows_of(orders)) == list(range(16, ROWS + 1))


def test_a_column_renamed_outside_the_guard_does_not_lock_the_table_either(
    make_guard, orders, scope
):
    with orders.table("orders").update_schema() as update:
        update.rename_column("customer", "buyer")
    result = make_guard().submit(SqlAction("DELETE FROM orders WHERE id = 1"), scope)
    assert result.outcome is Outcome.PUBLISHED, (result.reasons, result.error)


def test_a_row_diff_after_a_drop_shows_only_the_rows_not_a_phantom_schema_change(
    pguard, orders, scope, verifier
):
    decide(pguard, verifier, queue(pguard, scope, DROP), nonce="a")
    delete = pguard.submit(SqlAction("DELETE FROM orders WHERE id <= 15"), scope)
    diff = pguard.diff_for(delete.action_id)
    assert diff.schema_diff.empty and diff.rows_deleted == 15


@pytest.mark.parametrize("alter", [DROP, RENAME], ids=["drop", "rename"])
def test_a_pending_write_goes_stale_when_the_columns_changed_after_its_rehearsal(
    pguard, orders, scope, verifier, alter
):
    update = pguard.submit(
        SqlAction("UPDATE orders SET amount = amount + 100 WHERE id <= 15"), scope
    )
    assert update.outcome is Outcome.AWAITING_APPROVAL
    assert (
        decide(pguard, verifier, queue(pguard, scope, alter), nonce="a").outcome
        is Outcome.PUBLISHED
    )
    decided = decide(pguard, verifier, update, nonce="b")
    assert decided.outcome is Outcome.STALE
    assert any("columns changed" in r for r in decided.reasons)
    assert pguard.store.get(update.action_id).state is ActionState.STALE
    assert not [b for b in orders.refs("orders") if b != "main"]  # its audit branch is dropped


# -- what a plan refuses -----------------------------------------------------------------------


def denied(make_guard, scope, sql):
    return make_guard().submit(SqlAction(sql), scope, lambda r: True)


def test_a_column_the_sort_order_uses_cannot_be_dropped(make_guard, orders, scope):
    from pyiceberg.transforms import IdentityTransform

    with orders.table("orders").transaction() as tx:
        tx.update_sort_order().asc("amount", IdentityTransform()).commit()
    result = denied(make_guard, scope, DROP)
    assert result.outcome is Outcome.DENIED and "sort order" in " ".join(result.reasons)
    assert "amount" in names(orders)
    # renaming it is fine: the field id is what the sort order refers to
    assert denied(make_guard, scope, "ALTER TABLE orders RENAME COLUMN amount TO total").ok


def test_an_identifier_field_cannot_be_dropped(tmp_path):
    lake = LakehouseCatalog.local(tmp_path / "lake")
    schema = pa.schema([pa.field("id", pa.int64(), nullable=False), pa.field("v", pa.string())])
    table = lake.create_table("t", pa.table({"id": [1], "v": ["x"]}, schema=schema))
    with table.update_schema() as update:
        update.set_identifier_fields("id")
    with pytest.raises(SchemaChangeRefused, match="identifier"):
        DuckDBIcebergAdapter(lake).plan_schema_change("t", [ColumnChange("drop", "id")])


def test_an_error_the_catalog_raises_before_writing_fails_the_action_not_flags_it(
    pguard, orders, scope, verifier, monkeypatch
):
    from pyiceberg.exceptions import ValidationError

    result = queue(pguard, scope, DROP)

    def reject(self):
        raise ValidationError("Cannot find source column")

    monkeypatch.setattr(Transaction, "commit_transaction", reject)
    decided = decide(pguard, verifier, result)
    assert decided.outcome is Outcome.EXECUTION_FAILED
    assert pguard.store.get(result.action_id).state is ActionState.FAILED
    assert "amount" in names(orders)


@pytest.mark.parametrize(
    "sql",
    [
        "ALTER TABLE orders RENAME COLUMN customer TO AMOUNT",
        "ALTER TABLE orders RENAME COLUMN customer TO amount",
        "ALTER TABLE orders RENAME COLUMN customer TO customer",
        "ALTER TABLE orders DROP COLUMN id, DROP COLUMN customer, DROP COLUMN amount",
    ],
)
def test_a_plan_that_clashes_or_changes_nothing_or_empties_the_table_is_denied(
    make_guard, orders, scope, sql
):
    result = denied(make_guard, scope, sql)
    assert result.outcome is Outcome.DENIED and not result.executed
    assert names(orders) == ["id", "customer", "amount"]


def test_a_name_is_found_without_regard_to_case_and_applied_as_the_table_spells_it(
    pguard, orders, scope, verifier
):
    result = queue(pguard, scope, "ALTER TABLE orders RENAME COLUMN Customer TO buyer")
    assert pguard.store.get(result.action_id).impact_data["schema_change"]["renamed"] == [
        ["customer", "buyer"]
    ]
    assert decide(pguard, verifier, result).outcome is Outcome.PUBLISHED
    assert names(orders) == ["id", "buyer", "amount"]


def test_a_name_two_columns_share_up_to_case_must_be_written_exactly(tmp_path):
    lake = LakehouseCatalog.local(tmp_path / "lake")
    lake.create_table("t", pa.table({"Ab": [1], "aB": [2], "c": [3]}))
    adapter = DuckDBIcebergAdapter(lake)
    with pytest.raises(ExecutionError, match="ambiguous"):
        adapter.plan_schema_change("t", [ColumnChange("drop", "AB")])
    plan = adapter.plan_schema_change("t", [ColumnChange("drop", "aB")])
    assert [c.name for c in plan.after] == ["Ab", "c"]


# -- reading after a rename --------------------------------------------------------------------

READS = [
    "SELECT * FROM orders WHERE id = 3",
    "SELECT count(*) AS n FROM orders",
]


def externally_renamed(orders) -> None:
    with orders.table("orders").update_schema() as update:
        update.rename_column("customer", "buyer")


def approved_rename(make_guard, scope) -> None:
    assert make_guard().submit(SqlAction(RENAME), scope, lambda r: True).ok


@pytest.mark.parametrize("how", ["approved", "external"])
@pytest.mark.parametrize("read", READS)
def test_a_renamed_table_can_be_read_before_anything_writes_to_it(
    make_guard, orders, scope, how, read
):
    approved_rename(make_guard, scope) if how == "approved" else externally_renamed(orders)
    result = make_guard().submit(SqlAction(read), scope)
    assert result.outcome is Outcome.READ, (result.reasons, result.error)
    if read.startswith("SELECT *"):
        assert result.data.column_names == ["id", "buyer", "amount"] and result.data.num_rows == 1
    else:
        assert result.data.column("n").to_pylist() == [ROWS]


@pytest.mark.parametrize("how", ["approved", "external"])
def test_a_table_can_be_copied_from_a_renamed_one(make_guard, orders, scope, how):
    orders.create_table("other", pa.table({"id": pa.array([1], pa.int64())}))
    approved_rename(make_guard, scope) if how == "approved" else externally_renamed(orders)
    result = make_guard().submit(
        SqlAction("INSERT INTO other SELECT id + 1000 FROM orders WHERE id = 4"), scope
    )
    assert result.outcome is Outcome.PUBLISHED, (result.reasons, result.error)
    assert sorted(orders.read("other").column("id").to_pylist()) == [1, 1004]


def test_the_adapter_reads_a_renamed_table_with_its_current_names(make_guard, orders, scope):
    approved_rename(make_guard, scope)
    adapter = DuckDBIcebergAdapter(orders)
    assert adapter.read("orders").column_names == ["id", "buyer", "amount"]
    head = adapter.current_snapshot_id("orders")
    assert adapter.read("orders", head).column_names == ["id", "buyer", "amount"]


# -- partition specs that were replaced --------------------------------------------------------


def test_a_column_an_older_partition_spec_used_cannot_be_dropped(tmp_path):
    lake = LakehouseCatalog.local(tmp_path / "lake")
    table = lake.create_table(
        "t", pa.table({"id": pa.array([1], pa.int64()), "region": ["n"], "v": [1.0]})
    )
    with table.update_spec() as spec:
        spec.add_identity("region")
    lake.table("t").append(pa.table({"id": pa.array([2], pa.int64()), "region": ["s"], "v": [2.0]}))
    with lake.table("t").update_spec() as spec:
        spec.remove_field("region")
    assert not lake.table("t").spec().fields  # unpartitioned now; spec 1 still has files
    adapter = DuckDBIcebergAdapter(lake)
    with pytest.raises(SchemaChangeRefused, match="partition spec"):
        adapter.plan_schema_change("t", [ColumnChange("drop", "region")])
    plan = adapter.plan_schema_change("t", [ColumnChange("rename", "region", "area")])
    assert [c.name for c in plan.after] == ["id", "area", "v"]
