"""Policy v2: production namespaces always ask, and unkeyed updates are not published silently."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pyarrow as pa
import pytest

from sqlguard.core.findings import Decision
from sqlguard.safety import (
    ActionState,
    ActionStore,
    ExpireSnapshots,
    Guard,
    Impact,
    Policy,
    PolicyError,
    ReversibilityClass,
    SqlAction,
    TaskScope,
    classify,
)
from sqlguard.safety.guard import Outcome
from tests.safety.conftest import ROWS, order_ids

C = ReversibilityClass
ROOT = Path(__file__).resolve().parents[2]
V1 = ROOT / "config" / "policy.example.yaml"
V2 = ROOT / "config" / "policy.v2.yaml"
NOW = datetime(2026, 11, 4, tzinfo=UTC)
BOTH = TaskScope.of(namespaces=["db", "prod"])


@pytest.fixture(scope="module")
def v2() -> Policy:
    return Policy.load(V2)


def decide(policy: Policy, sql: str, scope: TaskScope = BOTH, impact: Impact | None = None):
    classification = classify(SqlAction(sql))
    if impact is not None:
        classification = classification.with_impact(impact)
    return policy.decide(classification, scope, now=NOW)


# -- production writes ---------------------------------------------------------------------------


def test_the_shipped_files_load_and_v1_keeps_its_behaviour(v2: Policy) -> None:
    v1 = Policy.load(V1)
    assert v1.environments == {} and v1.require_table_key is False
    assert v2.environments == {"db": "dev", "prod": "prod"} and v2.require_table_key is True
    one_row = "UPDATE prod.orders SET status = 'cancelled' WHERE id = 150"
    assert decide(v1, one_row).decision is Decision.ALLOW_VIA_BRANCH  # the case v2 closes
    assert decide(v2, one_row).decision is Decision.ASK


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE prod.orders SET status = 'cancelled' WHERE id = 150",
        "DELETE FROM prod.orders WHERE id = 1",
        "INSERT INTO prod.orders SELECT * FROM db.orders WHERE id = 1",
        "INSERT INTO prod.customers VALUES (1, 'a', 'b', 'c', DATE '2026-01-01')",
        "CREATE TABLE prod.scratch AS SELECT 1 AS x",
        "MERGE INTO prod.orders t USING db.orders s ON t.id = s.id WHEN MATCHED THEN DELETE",
    ],
)
def test_every_write_to_prod_asks_whatever_its_size(v2: Policy, sql: str) -> None:
    decided = decide(v2, sql, impact=Impact(rows_affected=1, total_rows=100_000))
    assert decided.decision is Decision.ASK
    assert any("production" in r and "prod." in r for r in decided.reasons)


def test_a_prod_write_asks_before_and_after_the_dry_run(v2: Policy) -> None:
    sql = "UPDATE prod.orders SET status = 'cancelled' WHERE id = 150"
    assert decide(v2, sql).decision is Decision.ASK
    assert decide(v2, sql, impact=Impact(1, 200, keyed=True)).decision is Decision.ASK


def test_dev_behaves_as_in_v1(v2: Policy) -> None:
    v1 = Policy.load(V1)
    for sql in (
        "SELECT * FROM orders",
        "INSERT INTO orders_staging SELECT * FROM orders WHERE id < 3",
        "DELETE FROM orders WHERE id = 1",
        "UPDATE orders SET status = 'x' WHERE id = 1",
        "DELETE FROM orders",
        "ALTER TABLE orders DROP COLUMN region",
        "COMMIT",
    ):
        assert decide(v2, sql).decision is decide(v1, sql).decision, sql
    small = decide(v2, "DELETE FROM orders WHERE id = 1", impact=Impact(1, 200, keyed=True))
    assert small.decision is Decision.ALLOW_VIA_BRANCH


def test_reading_prod_stays_allowed_and_a_prod_read_into_dev_is_a_dev_write(v2: Policy) -> None:
    assert decide(v2, "SELECT count(*) FROM prod.orders").decision is Decision.ALLOW
    copy = decide(v2, "INSERT INTO orders_staging SELECT * FROM prod.orders WHERE id < 3")
    assert copy.decision is Decision.ALLOW_VIA_BRANCH


def test_prod_is_still_denied_when_it_is_outside_the_task_scope(v2: Policy) -> None:
    decided = decide(v2, "DELETE FROM prod.orders WHERE id = 1", scope=TaskScope.of(["db.orders"]))
    assert decided.decision is Decision.DENY and decided.klass is C.OUT_OF_SCOPE


def test_a_retention_violation_on_prod_stays_a_denial(v2: Policy) -> None:
    classification = classify(ExpireSnapshots("prod.orders", older_than=NOW, retain_last=1))
    decided = v2.decide(classification, BOTH, now=NOW)
    assert decided.decision is Decision.DENY


def test_a_three_part_name_cannot_hide_a_prod_namespace() -> None:
    policy = Policy(
        environments={"prod": "prod"}, task_scope=TaskScope.of(namespaces=["lake.prod"])
    )
    assert policy.environment_of("lake.prod.orders") == "prod"
    assert policy.environment_of("db.orders") == "dev"
    decided = policy.decide(classify(SqlAction("DELETE FROM lake.prod.orders WHERE id = 1")))
    assert decided.decision is Decision.ASK


def test_environment_names_are_matched_case_insensitively() -> None:
    policy = Policy.from_dict({"environments": {"Prod": "PROD", " db ": "Dev"}})
    assert policy.environments == {"prod": "prod", "db": "dev"}


def test_a_typo_in_an_environment_is_an_error_not_a_silent_dev() -> None:
    with pytest.raises(PolicyError, match="environments must map to one of"):
        Policy.from_dict({"environments": {"prod": "production"}})
    with pytest.raises(PolicyError, match="environments must be a mapping"):
        Policy.from_dict({"environments": ["prod"]})
    with pytest.raises(PolicyError, match="twice"):
        Policy.from_dict({"environments": {"prod": "prod", "PROD": "dev"}})
    with pytest.raises(PolicyError, match="require_table_key must be true or false"):
        Policy.from_dict({"require_table_key": "yes"})


def test_the_digest_changes_with_the_new_rules_and_not_without_them(v2: Policy) -> None:
    plain = Policy()
    assert Policy.from_dict({}).digest == plain.digest
    assert Policy(environments={"prod": "prod"}).digest != plain.digest
    assert Policy(require_table_key=True).digest != plain.digest
    assert v2.digest != Policy.load(V1).digest


# -- unkeyed diffs -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE customers SET country = 'France' WHERE id = 3",
        "MERGE INTO customers t USING customers_staging s ON t.id = s.id "
        "WHEN MATCHED THEN UPDATE SET country = s.country",
    ],
)
def test_an_unkeyed_update_asks_with_the_reason(v2: Policy, sql: str) -> None:
    decided = decide(v2, sql, impact=Impact(rows_affected=1, total_rows=60, keyed=False))
    assert decided.decision is Decision.ASK
    assert any(r.startswith("unkeyed diff") for r in decided.reasons)


def test_a_keyed_update_and_an_update_nobody_looked_at_are_not_asked_about(v2: Policy) -> None:
    sql = "UPDATE customers SET country = 'France' WHERE id = 3"
    for impact in (Impact(1, 60, keyed=True), Impact(1, 60), None):
        assert decide(v2, sql, impact=impact).decision is Decision.ALLOW_VIA_BRANCH


def test_a_statement_that_changed_nothing_is_not_held_for_a_missing_key(v2: Policy) -> None:
    sql = "UPDATE customers SET country = 'France' WHERE id = 3"
    nothing = decide(v2, sql, impact=Impact(rows_affected=0, total_rows=60, keyed=False))
    assert nothing.decision is Decision.ALLOW_VIA_BRANCH


def test_inserts_and_deletes_need_no_key(v2: Policy) -> None:
    for sql in ("DELETE FROM customers WHERE id = 3", "INSERT INTO customers SELECT * FROM events"):
        decided = decide(v2, sql, impact=Impact(1, 60, keyed=False))
        assert decided.decision is Decision.ALLOW_VIA_BRANCH, sql


def test_v1_ignores_the_key_signal() -> None:
    sql = "UPDATE customers SET country = 'France' WHERE id = 3"
    v1 = Policy.load(V1)
    assert decide(v1, sql, impact=Impact(1, 60, keyed=False)).decision is Decision.ALLOW_VIA_BRANCH


# -- through the guard ---------------------------------------------------------------------------


@pytest.fixture
def prod_orders(orders):
    orders.catalog.create_namespace_if_not_exists("prod")
    orders.create_table(
        "prod.orders",
        pa.table(
            {
                "id": pa.array(range(1, ROWS + 1), pa.int64()),
                "customer": pa.array([f"c{i % 4}" for i in range(ROWS)]),
                "amount": pa.array([float(i) for i in range(ROWS)]),
            }
        ),
    )
    return orders


def guard_v2(catalog, tmp_path: Path, **kwargs) -> Guard:
    return Guard(
        catalog,
        Policy.from_dict(
            {
                "environments": {"db": "dev", "prod": "prod"},
                "require_table_key": True,
                "invariants": {"tables": {"orders": {"unique_key": ["id"]}}},
            }
        ),
        audit_path=tmp_path / "audit.jsonl",
        store=ActionStore(tmp_path / "actions.db"),
        **kwargs,
    )


def test_a_one_row_prod_update_is_rehearsed_queued_and_leaves_the_table_alone(
    prod_orders, tmp_path: Path
) -> None:
    guard = guard_v2(prod_orders, tmp_path)
    before = prod_orders.table("prod.orders").current_snapshot().snapshot_id
    result = guard.submit(SqlAction("UPDATE prod.orders SET amount = 0 WHERE id = 7"), BOTH)
    assert result.outcome is Outcome.AWAITING_APPROVAL and result.action_id
    assert not result.published and result.impact is not None
    assert any("production" in r for r in result.reasons)
    assert prod_orders.table("prod.orders").current_snapshot().snapshot_id == before
    assert guard.store.get(result.action_id).state is ActionState.PENDING


def test_the_same_update_on_dev_is_published_without_asking(prod_orders, tmp_path: Path) -> None:
    guard = guard_v2(prod_orders, tmp_path)
    result = guard.submit(SqlAction("UPDATE orders SET amount = 0 WHERE id = 7"), BOTH)
    assert result.outcome is Outcome.PUBLISHED and result.published


def test_an_unkeyed_dev_update_is_queued_with_the_reason_and_a_keyed_one_is_not(
    prod_orders, tmp_path: Path
) -> None:
    prod_orders.create_table(
        "notes", pa.table({"id": pa.array(range(1, 41), pa.int64()), "text": ["a"] * 40})
    )
    guard = guard_v2(prod_orders, tmp_path)
    unkeyed = guard.submit(SqlAction("UPDATE notes SET text = 'z' WHERE id = 1"), BOTH)
    assert unkeyed.outcome is Outcome.AWAITING_APPROVAL and unkeyed.action_id
    assert any("unkeyed diff" in r for r in unkeyed.reasons)
    assert set(prod_orders.read("notes").column("text").to_pylist()) == {"a"}

    keyed = guard.submit(SqlAction("UPDATE orders SET amount = 99 WHERE id = 2"), BOTH)
    assert keyed.outcome is Outcome.PUBLISHED
    assert order_ids(prod_orders) == list(range(1, ROWS + 1))


def test_declaring_the_key_in_the_policy_makes_the_update_automatic(
    prod_orders, tmp_path: Path
) -> None:
    prod_orders.create_table(
        "notes", pa.table({"id": pa.array(range(1, 41), pa.int64()), "text": ["a"] * 40})
    )
    guard = Guard(
        prod_orders,
        Policy.from_dict(
            {
                "require_table_key": True,
                "invariants": {"tables": {"notes": {"unique_key": ["id"]}}},
            }
        ),
        audit_path=tmp_path / "audit.jsonl",
        store=ActionStore(tmp_path / "actions.db"),
    )
    result = guard.submit(SqlAction("UPDATE notes SET text = 'z' WHERE id = 1"), BOTH)
    assert result.outcome is Outcome.PUBLISHED
