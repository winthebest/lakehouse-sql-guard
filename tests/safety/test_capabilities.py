"""Executors declare what they can do; the Guard refuses what they cannot do safely."""

from __future__ import annotations

import dataclasses

import pytest

from sqlguard.core.findings import Decision
from sqlguard.safety import Policy, SqlAction, TaskScope
from sqlguard.safety.executor import Capabilities, DuckDBIcebergAdapter, SparkIcebergAdapter
from sqlguard.safety.guard import Guard, Outcome
from tests.safety.conftest import ROWS, main_ids, order_ids


def sql(text: str) -> SqlAction:
    return SqlAction(text)


class ReadOnlyEngine(DuckDBIcebergAdapter):
    """An engine that can query main but cannot write to an isolated branch (like Trino today)."""

    engine = "readonly-engine"
    capabilities = Capabilities(publish=False, rollback=False)


@pytest.fixture
def readonly_guard(orders, audit_path) -> Guard:
    return Guard(orders, Policy(), audit_path=audit_path, adapter=ReadOnlyEngine(orders))


def test_capabilities_are_frozen_and_opt_in():
    nothing = Capabilities()
    assert not any(dataclasses.asdict(nothing).values())
    assert all(dataclasses.asdict(Capabilities.full()).values())
    assert [f.name for f in dataclasses.fields(Capabilities)] == [
        "branch_write",
        "publish",
        "rollback",
        "expire",
        "orphan_cleanup",
        "changelog",
    ]
    with pytest.raises(dataclasses.FrozenInstanceError):
        nothing.branch_write = True  # type: ignore[misc]


def test_shipped_adapters_declare_what_they_do(orders):
    for adapter, engine in (
        (DuckDBIcebergAdapter(orders), "duckdb"),
        (SparkIcebergAdapter(orders, object(), "demo"), "spark"),
    ):
        assert adapter.engine == engine
        caps = adapter.capabilities
        assert caps.branch_write and caps.publish and caps.rollback
        assert caps.expire and caps.orphan_cleanup
        assert not caps.changelog  # neither adapter reads a change feed


@pytest.mark.parametrize(
    "statement",
    [
        "DELETE FROM orders WHERE id = 1",  # would be rehearsed and published
        "DELETE FROM orders",  # would need an approver after the rehearsal
        "INSERT INTO orders VALUES (100, 'x', 1.0)",
    ],
)
def test_writes_are_denied_when_there_is_no_isolated_rehearsal(
    readonly_guard, orders, scope, audit, statement
):
    before = main_ids(orders)
    result = readonly_guard.submit(sql(statement), scope, lambda request: True)

    assert result.outcome is Outcome.DENIED and result.decision is Decision.DENY
    assert "no isolated rehearsal on readonly-engine" in result.reasons
    assert not result.executed and not result.published
    assert main_ids(orders) == before and set(orders.refs("orders")) == {"main"}  # no branch cut
    records = audit.read()
    assert [r["phase"] for r in records] == ["completed"]  # nothing started, so no intent record
    assert records[0]["decision"] == "deny"


def test_the_denial_does_not_wait_for_an_approver(readonly_guard, scope):
    result = readonly_guard.submit(sql("DELETE FROM orders"), scope)  # no approver given
    assert result.outcome is Outcome.DENIED


def test_reads_still_work_without_branch_writes(readonly_guard, orders, scope):
    result = readonly_guard.submit(sql("SELECT count(*) AS n FROM orders"), scope)
    assert result.outcome is Outcome.READ
    assert result.data.column("n").to_pylist() == [ROWS]


def test_a_policy_denial_keeps_its_own_reasons(readonly_guard):
    out_of_scope = TaskScope.of(tables=["db.other"])
    result = readonly_guard.submit(sql("DELETE FROM orders WHERE id = 1"), out_of_scope)
    assert result.outcome is Outcome.DENIED
    assert "no isolated rehearsal on readonly-engine" not in result.reasons


def test_an_adapter_without_capabilities_is_assumed_capable(orders, audit_path, scope):
    class Legacy(DuckDBIcebergAdapter):
        capabilities = None  # an adapter written before capabilities existed

    guard = Guard(orders, Policy(), audit_path=audit_path, adapter=Legacy(orders))
    result = guard.submit(sql("DELETE FROM orders WHERE id = 1"), scope)
    assert result.outcome is Outcome.PUBLISHED
    assert order_ids(orders) == list(range(2, ROWS + 1))
