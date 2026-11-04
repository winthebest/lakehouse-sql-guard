"""TrinoIcebergAdapter: SQL building, Arrow conversion and control flow against a fake Trino.

`FakeTrino` answers the handful of statement shapes the adapter sends by acting on a real local
Iceberg table with PyIceberg, so ancestry checks, snapshot logs and expiry run against real table
metadata. What Trino 484 itself does is locked by `tests/integration/test_trino.py` (Docker).
"""

from __future__ import annotations

import re
import sys
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import pyarrow as pa
import pytest
from pyiceberg.table import Table
from pyiceberg.table.update import SetSnapshotRefUpdate
from sqlglot import parse_one

from sqlguard.core.catalog import LakehouseCatalog
from sqlguard.safety.actions import (
    DropTable,
    ExpireSnapshots,
    FastForward,
    RemoveOrphanFiles,
    RollbackToSnapshot,
)
from sqlguard.safety.executor import (
    BranchDiverged,
    BranchHandle,
    Capabilities,
    ExecutionError,
    UnsupportedAction,
    trino_iceberg,
)
from sqlguard.safety.executor.trino_iceberg import (
    MaintenanceNeedsAttention,
    RollbackNeedsAttention,
    TrinoIcebergAdapter,
    arrow_type,
    read_statement,
    rows_to_arrow,
    table_identifier,
    trino_statement,
)

REAL_NOW = datetime.now(UTC)
FUTURE = REAL_NOW + timedelta(days=2)
CUTOFF = REAL_NOW + timedelta(days=1)  # every snapshot of a test table is older than this


# -- a Trino stand-in -----------------------------------------------------------------------------

_TRINO_TYPES = {
    pa.int64(): "bigint",
    pa.int32(): "integer",
    pa.string(): "varchar",
    pa.large_string(): "varchar",
    pa.float64(): "double",
}


def _description(schema: pa.Schema) -> list[tuple[str, str]]:
    out = []
    for field in schema:
        trino_type = (
            "timestamp(6)" if pa.types.is_timestamp(field.type) else _TRINO_TYPES[field.type]
        )
        out.append((field.name, trino_type))
    return out


class FakeTrino:
    """Connection + cursor in one; `statements` records everything it was asked to run."""

    def __init__(self, lake: LakehouseCatalog, now: datetime | None = None) -> None:
        self.lake = lake
        self.now = now or datetime.now(UTC)
        self.statements: list[str] = []
        self.canned: list[tuple[str, tuple[list[tuple[str, str]], list[list[Any]]]]] = []
        self.fail_on: tuple[str, str] | None = None
        self.before_rollback: Callable[[], None] | None = None
        self.after_rollback: Callable[[], None] | None = None
        self.overreach = False  # expire everything but the current snapshot, cutoff or not
        self.before_expire: Callable[[], None] | None = None
        self.description: list[tuple[str, str]] | None = None
        self._rows: list[list[Any]] = []

    # connection
    def cursor(self) -> FakeTrino:
        return self

    def close(self) -> None:
        pass

    # cursor
    def execute(self, text: str, params: Any = None) -> None:
        self.statements.append(text)
        self.description, self._rows = None, []
        if self.fail_on and self.fail_on[0] in text:
            raise RuntimeError(self.fail_on[1])
        for needle, (description, rows) in self.canned:
            if needle in text:
                self.description, self._rows = description, rows
                return
        if match := re.fullmatch(
            r'SELECT \* FROM "lk"\."db"\."(\w+)"(?: FOR VERSION AS OF (?:(\d+)|\'([^\']+)\'))?',
            text,
        ):
            self._select(*match.groups())
        elif match := re.fullmatch(
            r'SELECT name, snapshot_id FROM "lk"\."db"\."(\w+)\$refs"', text
        ):
            refs = self.lake.refs(match[1])
            self.description = [("name", "varchar"), ("snapshot_id", "bigint")]
            self._rows = [[name, snapshot] for name, snapshot in refs.items()]
        elif match := re.fullmatch(
            r'SELECT snapshot_id, parent_id FROM "lk"\."db"\."(\w+)\$snapshots" '
            "ORDER BY committed_at",
            text,
        ):
            self.description = [("snapshot_id", "bigint"), ("parent_id", "bigint")]
            self._rows = [[s.snapshot_id, s.parent_id] for s in self.lake.snapshots(match[1])]
        elif match := re.fullmatch(
            r'ALTER TABLE "lk"\."db"\."(\w+)" EXECUTE rollback_to_snapshot\(snapshot_id => (\d+)\)',
            text,
        ):
            self._rollback(match[1], int(match[2]))
        elif match := re.fullmatch(
            r'ALTER TABLE "lk"\."db"\."(\w+)" EXECUTE expire_snapshots\('
            r"retention_threshold => '(\d+)s'\)",
            text,
        ):
            self._expire(match[1], int(match[2]))
        else:
            raise AssertionError(f"FakeTrino got an unexpected statement: {text}")

    def fetchmany(self, size: int = 1) -> list[list[Any]]:
        taken, self._rows = self._rows[:size], self._rows[size:]
        return taken

    def fetchall(self) -> list[list[Any]]:
        taken, self._rows = self._rows, []
        return taken

    def altered(self) -> list[str]:
        return [s for s in self.statements if s.startswith("ALTER")]

    # effects
    def _select(self, name: str, snapshot_id: str | None, ref: str | None) -> None:
        table = self.lake.table(name)
        if ref is not None:
            if ref not in table.refs():
                raise RuntimeError(f"Cannot find snapshot with reference name: {ref}")
            snapshot_id = str(table.refs()[ref].snapshot_id)
        data = self.lake.read(name, int(snapshot_id) if snapshot_id else None)
        self.description = _description(data.schema)
        self._rows = [list(row.values()) for row in data.to_pylist()]

    def _rollback(self, name: str, target: int) -> None:
        if self.before_rollback:
            self.before_rollback()
        # Like Trino: any snapshot id is accepted, ancestor or not.
        self.lake.table(name).manage_snapshots().set_current_snapshot(snapshot_id=target).commit()
        if self.after_rollback:
            self.after_rollback()

    def _expire(self, name: str, threshold_s: int) -> None:
        """Iceberg's expiry as documented, written independently of the adapter's planner:
        refs past their max ref age are dropped first (a ref's own setting, else the table's
        `history.expire.max-ref-age-ms`), then snapshots older than the cutoff that no remaining
        ref points to are expired."""
        if self.before_expire:
            self.before_expire()
        table = self.lake.table(name)
        now_ms = int(self.now.timestamp() * 1000)
        cutoff = now_ms - threshold_s * 1000
        default_age = int(table.properties.get("history.expire.max-ref-age-ms", 2**62))
        for ref_name, ref in list(table.refs().items()):
            if ref_name == "main":
                continue
            head_age = now_ms - table.snapshot_by_id(ref.snapshot_id).timestamp_ms
            limit = ref.max_ref_age_ms if ref.max_ref_age_ms is not None else default_age
            if head_age > limit:
                manage = table.manage_snapshots()
                (manage.remove_branch if ref.snapshot_ref_type == "branch" else manage.remove_tag)(
                    ref_name
                ).commit()
        table = self.lake.table(name)
        kept = {ref.snapshot_id for ref in table.refs().values()}
        doomed = [
            s.snapshot_id
            for s in table.snapshots()
            if s.timestamp_ms < cutoff and s.snapshot_id not in kept
        ]
        if self.overreach:
            main = table.current_snapshot().snapshot_id
            doomed = [s.snapshot_id for s in table.snapshots() if s.snapshot_id != main]
        if doomed:
            table.maintenance.expire_snapshots().by_ids(doomed).commit()


def row(i: int) -> pa.Table:
    return pa.table(
        {"id": pa.array([i], pa.int64()), "customer": ["z"], "amount": pa.array([float(i)])}
    )


@pytest.fixture
def trino(orders: LakehouseCatalog) -> FakeTrino:
    return FakeTrino(orders, now=FUTURE)


@pytest.fixture
def adapter(orders: LakehouseCatalog, trino: FakeTrino) -> TrinoIcebergAdapter:
    return TrinoIcebergAdapter(orders, trino, "lk")


@pytest.fixture
def history(orders: LakehouseCatalog) -> list[int]:
    """Two more snapshots on main: returns the ids oldest first (3 in total)."""
    orders.table("orders").append(row(100))
    orders.table("orders").append(row(101))
    return [s.snapshot_id for s in orders.snapshots("orders")]


# -- SQL text -------------------------------------------------------------------------------------


def test_identifiers_are_quoted_and_odd_names_refused() -> None:
    assert table_identifier("lk", "db.orders") == '"lk"."db"."orders"'
    assert table_identifier("lk", "db.orders", suffix="$refs") == '"lk"."db"."orders$refs"'
    for bad in ("orders", 'db.or"ders', "db.or ders", "db.x;y", "a.b.c"):
        with pytest.raises(ExecutionError):
            table_identifier("lk", bad)
    with pytest.raises(ExecutionError):
        table_identifier("lk", "db.orders", suffix="$files; DROP")


def test_read_statements_pin_a_snapshot_or_a_named_ref() -> None:
    assert read_statement("lk", "db.orders") == 'SELECT * FROM "lk"."db"."orders"'
    assert read_statement("lk", "db.orders", snapshot_id=7) == (
        'SELECT * FROM "lk"."db"."orders" FOR VERSION AS OF 7'
    )
    assert read_statement("lk", "db.orders", ref="sqlguard-ab12") == (
        'SELECT * FROM "lk"."db"."orders" FOR VERSION AS OF \'sqlguard-ab12\''
    )
    for bad in ("x' OR '1", "a b", "", "1abc"):
        with pytest.raises(ExecutionError):
            read_statement("lk", "db.orders", ref=bad)
    with pytest.raises(ExecutionError):
        read_statement("lk", "db.orders", snapshot_id=1, ref="b")


def render(sql: str, readable: tuple[str, ...] = ("db.orders",)) -> str:
    return trino_statement(
        parse_one(sql, dialect="duckdb"),
        trino_catalog="lk",
        default_namespace="db",
        readable=readable,
    )


def test_queries_are_qualified_to_the_trino_catalog() -> None:
    assert render("SELECT o.id FROM orders o WHERE o.id > 3") == (
        'SELECT o.id FROM "lk"."db"."orders" AS o WHERE o.id > 3'
    )
    sql = render(
        "WITH big AS (SELECT * FROM orders WHERE amount > 5) SELECT * FROM big JOIN db.customers c"
        " ON c.id = big.id",
        readable=("db.orders", "db.customers"),
    )
    assert 'FROM "lk"."db"."orders"' in sql and 'JOIN "lk"."db"."customers"' in sql
    assert "FROM big" in sql


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM secrets",
        "SELECT * FROM other.orders",
        "SELECT * FROM lake.db.orders",
        "SELECT * FROM orders WHERE id IN (SELECT id FROM audit.secrets)",
    ],
)
def test_tables_that_were_not_cleared_are_refused(sql: str) -> None:
    with pytest.raises(ExecutionError):
        render(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM TABLE(pg.system.query(query => 'select 1'))",
        "SELECT * FROM TABLE(system.query(query => 'select 1'))",
        "SELECT * FROM TABLE(lk.system.table_changes(schema_name => 'db', table_name => 'secrets', "
        "start_snapshot_id => 1, end_snapshot_id => 2))",
        "SELECT * FROM read_parquet('x')",
        "SELECT * FROM generate_series(1, 3)",
        "SELECT * FROM orders o JOIN TABLE(system.query(query => 'x')) q ON true",
    ],
)
def test_table_functions_are_refused(sql: str) -> None:
    with pytest.raises(ExecutionError, match="table function"):
        render(sql)


def test_unnest_and_values_are_not_table_functions() -> None:
    assert "UNNEST" in render("SELECT x FROM orders CROSS JOIN UNNEST(ARRAY[1, 2]) AS u(x)")
    assert "VALUES" in render("SELECT * FROM (VALUES (1)) AS t(x)")


def test_a_table_function_never_reaches_trino(
    adapter: TrinoIcebergAdapter, trino: FakeTrino
) -> None:
    with pytest.raises(ExecutionError, match="table function"):
        adapter.query("SELECT * FROM TABLE(pg.system.query(query => 'select 1'))", ["orders"])
    assert trino.statements == []


def test_identifiers_with_a_trailing_newline_are_refused() -> None:
    with pytest.raises(ExecutionError):
        table_identifier("lk", "db.orders\n")
    with pytest.raises(ExecutionError):
        read_statement("lk", "db.orders", ref="branch\n")


# -- Trino results to Arrow -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("trino_type", "expected"),
    [
        ("bigint", pa.int64()),
        ("integer", pa.int32()),
        ("smallint", pa.int16()),
        ("tinyint", pa.int8()),
        ("real", pa.float32()),
        ("double", pa.float64()),
        ("boolean", pa.bool_()),
        ("varchar", pa.string()),
        ("varchar(20)", pa.string()),
        ("char(4)", pa.string()),
        ("uuid", pa.string()),
        ("varbinary", pa.binary()),
        ("date", pa.date32()),
        ("time(6)", pa.time64("us")),
        ("timestamp(6)", pa.timestamp("us")),
        ("timestamp(3)", pa.timestamp("us")),
        ("timestamp(6) with time zone", pa.timestamp("us", tz="UTC")),
        ("decimal(10, 2)", pa.decimal128(10, 2)),
        ("decimal(38,0)", pa.decimal128(38, 0)),
    ],
)
def test_the_type_map_is_explicit(trino_type: str, expected: pa.DataType) -> None:
    assert arrow_type(trino_type) == expected


@pytest.mark.parametrize(
    "trino_type",
    [
        "timestamp(9)",
        "timestamp(12) with time zone",
        "time(9)",
        "array(integer)",
        "map(varchar, integer)",
        "row(a integer)",
        "json",
        "ipaddress",
        "time(6) with time zone",
        "decimal(39, 2)",
        "interval day to second",
        "",
    ],
)
def test_unsupported_types_are_not_guessed(trino_type: str) -> None:
    assert arrow_type(trino_type) is None
    with pytest.raises(ExecutionError, match="cannot convert column 'c'"):
        rows_to_arrow([("c", trino_type)], [])


def test_rows_to_arrow_converts_values_and_keeps_types_without_rows() -> None:
    description = [
        ("id", "bigint"),
        ("name", "varchar"),
        ("price", "decimal(10, 2)"),
        ("at", "timestamp(6) with time zone"),
        ("ref", "uuid"),
    ]
    moment = datetime(2026, 1, 2, 3, 4, 5, 123456, tzinfo=UTC)
    uid = UUID("12345678-1234-1234-1234-123456789012")
    table = rows_to_arrow(description, [[1, "a", Decimal("1.25"), moment, uid], [None] * 5])
    assert table.schema.types == [
        pa.int64(),
        pa.string(),
        pa.decimal128(10, 2),
        pa.timestamp("us", tz="UTC"),
        pa.string(),
    ]
    assert table.column("at").to_pylist() == [moment, None]
    assert table.column("ref").to_pylist() == [str(uid), None]
    empty = rows_to_arrow(description, [])
    assert empty.num_rows == 0 and empty.schema == table.schema


def test_a_value_that_does_not_fit_its_type_is_an_error() -> None:
    with pytest.raises(ExecutionError, match="cannot convert column 'id'"):
        rows_to_arrow([("id", "bigint")], [["not a number"]])


# -- the adapter: reads ---------------------------------------------------------------------------


def test_capabilities_say_what_trino_does_and_does_not() -> None:
    assert TrinoIcebergAdapter.engine == "trino"
    assert TrinoIcebergAdapter.capabilities == Capabilities(
        branch_write=False,
        publish=False,
        rollback=True,
        expire=True,
        orphan_cleanup=False,
        changelog=True,
    )


def test_reads_main_a_snapshot_and_a_branch(
    adapter: TrinoIcebergAdapter, orders: LakehouseCatalog, trino: FakeTrino
) -> None:
    base = adapter.current_snapshot_id("orders")
    assert adapter.read("orders").num_rows == 20
    assert trino.statements[-1] == 'SELECT * FROM "lk"."db"."orders"'

    orders.table("orders").manage_snapshots().create_branch(base, "feature").commit()
    orders.table("orders").append(row(500), branch="feature")
    assert adapter.read("orders", base).num_rows == 20
    assert trino.statements[-1].endswith(f"FOR VERSION AS OF {base}")
    assert adapter.read_ref("orders", "feature").num_rows == 21
    assert trino.statements[-1].endswith("FOR VERSION AS OF 'feature'")
    assert adapter.read("orders").num_rows == 20  # the branch never reached main


def test_read_of_a_missing_table_does_not_reach_trino(
    adapter: TrinoIcebergAdapter, trino: FakeTrino
) -> None:
    with pytest.raises(ExecutionError, match="does not exist"):
        adapter.read("nope")
    assert trino.statements == []


def test_the_row_cap_is_a_hard_limit_not_a_truncation(
    orders: LakehouseCatalog, trino: FakeTrino
) -> None:
    capped = TrinoIcebergAdapter(orders, trino, "lk", max_rows=19)
    with pytest.raises(ExecutionError, match="more than 19 rows"):
        capped.read("orders")
    assert TrinoIcebergAdapter(orders, trino, "lk", max_rows=20).read("orders").num_rows == 20
    with pytest.raises(ValueError):
        TrinoIcebergAdapter(orders, trino, "lk", max_rows=0)


def test_trino_errors_are_wrapped_and_clipped(
    adapter: TrinoIcebergAdapter, trino: FakeTrino
) -> None:
    trino.fail_on = ("SELECT", "boom " + "x" * 2000)
    with pytest.raises(ExecutionError) as caught:
        adapter.read("orders")
    assert str(caught.value).startswith("Trino rejected the statement: boom")
    assert len(str(caught.value)) < 700


def test_query_runs_read_only_sql_qualified_to_the_catalog(
    adapter: TrinoIcebergAdapter, trino: FakeTrino
) -> None:
    trino.canned.append(("COUNT(*)", ([("n", "bigint")], [[20]])))
    result = adapter.query("SELECT count(*) AS n FROM orders", ["orders"])
    assert result.column("n").to_pylist() == [20] and result.schema.field("n").type == pa.int64()
    assert trino.statements[-1] == 'SELECT COUNT(*) AS n FROM "lk"."db"."orders"'


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM orders WHERE id = 1",
        "INSERT INTO orders VALUES (1, 'a', 1.0)",
        "UPDATE orders SET amount = 0",
        "DROP TABLE orders",
        "SELECT 1; DELETE FROM orders",
        "EXPLAIN DELETE FROM orders",
        "EXPLAIN ANALYZE SELECT * FROM orders",
        "CALL lk.system.rollback_to_snapshot('db', 'orders', 1)",
    ],
)
def test_only_reads_reach_trino(adapter: TrinoIcebergAdapter, trino: FakeTrino, sql: str) -> None:
    with pytest.raises(UnsupportedAction):
        adapter.query(sql, ["orders"])
    assert trino.statements == []


def test_refs_and_snapshots_are_trinos_own_view(
    adapter: TrinoIcebergAdapter, orders: LakehouseCatalog, history: list[int]
) -> None:
    orders.table("orders").manage_snapshots().create_branch(history[0], "old").commit()
    assert adapter.refs("orders") == {"main": history[-1], "old": history[0]}
    assert [s for s, _ in adapter.engine_snapshots("orders")] == history
    assert adapter.engine_snapshots("orders")[1] == (history[1], history[0])


def test_changes_asks_for_table_changes(
    adapter: TrinoIcebergAdapter, trino: FakeTrino, history: list[int]
) -> None:
    trino.canned.append(
        ("table_changes", ([("id", "bigint"), ("_change_type", "varchar")], [[100, "insert"]]))
    )
    result = adapter.changes("orders", history[0], history[1])
    assert result.column("_change_type").to_pylist() == ["insert"]
    assert trino.statements[-1] == (
        "SELECT * FROM TABLE(\"lk\".system.table_changes(schema_name => 'db', "
        f"table_name => 'orders', start_snapshot_id => {history[0]}, "
        f"end_snapshot_id => {history[1]}))"
    )


# -- the adapter: no branch writes ----------------------------------------------------------------


def test_trino_never_rehearses_or_publishes(adapter: TrinoIcebergAdapter, trino: FakeTrino) -> None:
    branch = BranchHandle("db.orders", "sqlguard-x", 1)
    with pytest.raises(UnsupportedAction):
        adapter.create_branch("orders")
    with pytest.raises(UnsupportedAction):
        adapter.apply_sql(branch, "DELETE FROM orders WHERE id = 1")
    with pytest.raises(UnsupportedAction):
        adapter.publish(branch, 2)
    with pytest.raises(UnsupportedAction):
        adapter.drop_branch(branch)
    assert trino.statements == []


def test_diff_measures_a_branch_written_elsewhere_by_the_head_trino_sees(
    adapter: TrinoIcebergAdapter, orders: LakehouseCatalog
) -> None:
    base = adapter.current_snapshot_id("orders")
    orders.table("orders").manage_snapshots().create_branch(base, "feature").commit()
    orders.table("orders").delete("id > 15", branch="feature")
    head = orders.table("orders").refs()["feature"].snapshot_id

    diff = adapter.diff(BranchHandle("db.orders", "feature", base), key=("id",))
    assert diff.impact.rows_deleted == 5
    assert diff.branch.head_snapshot_id == head and diff.branch.base_snapshot_id == base
    assert diff.before.num_rows == 20 and diff.after.num_rows == 15

    with pytest.raises(ExecutionError, match="does not see branch 'ghost'"):
        adapter.diff(BranchHandle("db.orders", "ghost", base))


# -- rollback -------------------------------------------------------------------------------------


def test_rollback_moves_main_and_checks_it_afterwards(
    adapter: TrinoIcebergAdapter, orders: LakehouseCatalog, trino: FakeTrino, history: list[int]
) -> None:
    adapter.rollback("orders", history[0], expected_current=history[-1])
    assert orders.table("orders").current_snapshot().snapshot_id == history[0]
    assert trino.altered() == [
        f'ALTER TABLE "lk"."db"."orders" EXECUTE rollback_to_snapshot(snapshot_id => {history[0]})'
    ]


def test_rollback_refuses_when_main_is_not_where_the_caller_expects(
    adapter: TrinoIcebergAdapter, trino: FakeTrino, history: list[int]
) -> None:
    with pytest.raises(BranchDiverged):
        adapter.rollback("orders", history[0], expected_current=history[1])
    assert trino.altered() == []


def test_rollback_checks_ancestry_because_trino_does_not(
    adapter: TrinoIcebergAdapter, orders: LakehouseCatalog, trino: FakeTrino, history: list[int]
) -> None:
    orders.table("orders").manage_snapshots().create_branch(history[0], "side").commit()
    orders.table("orders").append(row(900), branch="side")
    elsewhere = orders.table("orders").refs()["side"].snapshot_id
    with pytest.raises(ExecutionError, match="not an ancestor"):
        adapter.rollback("orders", elsewhere)
    with pytest.raises(ExecutionError, match="unknown snapshot"):
        adapter.rollback("orders", 12345)
    assert trino.altered() == []
    assert orders.table("orders").current_snapshot().snapshot_id == history[-1]


def test_rollback_to_the_current_snapshot_is_a_no_op(
    adapter: TrinoIcebergAdapter, trino: FakeTrino, history: list[int]
) -> None:
    adapter.rollback("orders", history[-1], expected_current=history[-1])
    assert trino.altered() == []


def test_a_writer_who_slips_in_before_trino_loads_the_table_is_restored(
    adapter: TrinoIcebergAdapter, orders: LakehouseCatalog, trino: FakeTrino, history: list[int]
) -> None:
    def writer() -> None:
        orders.table("orders").append(row(777))

    trino.before_rollback = writer
    with pytest.raises(BranchDiverged, match="newer snapshot was restored"):
        adapter.rollback("orders", history[0], expected_current=history[-1])
    ids = orders.read("orders").column("id").to_pylist()
    assert 777 in ids and 101 in ids  # the writer's commit is live again, nothing was lost


def test_rollback_without_an_expected_snapshot_protects_a_racing_writer(
    adapter: TrinoIcebergAdapter, orders: LakehouseCatalog, trino: FakeTrino, history: list[int]
) -> None:
    trino.before_rollback = lambda: orders.table("orders").append(row(777))
    with pytest.raises(BranchDiverged, match="newer snapshot was restored"):
        adapter.rollback("orders", history[0])
    assert 777 in orders.read("orders").column("id").to_pylist()


def test_two_writers_around_the_rollback_name_the_commit_that_left_main(
    adapter: TrinoIcebergAdapter, orders: LakehouseCatalog, trino: FakeTrino, history: list[int]
) -> None:
    trino.before_rollback = lambda: orders.table("orders").append(row(777))
    trino.after_rollback = lambda: orders.table("orders").append(row(888))
    slipped: list[int] = []
    trino.before_rollback = lambda: (
        orders.table("orders").append(row(777)),
        slipped.append(orders.table("orders").current_snapshot().snapshot_id),
    )
    with pytest.raises(BranchDiverged) as caught:
        adapter.rollback("orders", history[0], expected_current=history[-1])
    message = str(caught.value)
    assert "left as is" in message and "no longer on main" in message
    assert f"replaced snapshot {slipped[0]}" in message
    assert 888 in orders.read("orders").column("id").to_pylist()
    assert orders.table("orders").snapshot_by_id(slipped[0]) is not None


def test_a_branch_advancing_during_a_rollback_is_not_an_alarm(
    adapter: TrinoIcebergAdapter, orders: LakehouseCatalog, trino: FakeTrino, history: list[int]
) -> None:
    orders.table("orders").manage_snapshots().create_branch(history[0], "feature").commit()
    trino.after_rollback = lambda: orders.table("orders").append(row(10), branch="feature")
    adapter.rollback("orders", history[1], expected_current=history[-1])
    assert orders.table("orders").current_snapshot().snapshot_id == history[1]


def test_a_branch_dropped_during_a_rollback_is_reported_as_executed(
    adapter: TrinoIcebergAdapter, orders: LakehouseCatalog, trino: FakeTrino, history: list[int]
) -> None:
    orders.table("orders").manage_snapshots().create_branch(history[0], "feature").commit()
    trino.after_rollback = lambda: (
        orders.table("orders").manage_snapshots().remove_branch("feature").commit()
    )
    with pytest.raises(MaintenanceNeedsAttention, match="feature") as caught:
        adapter.rollback("orders", history[1], expected_current=history[-1])
    assert caught.value.executed
    assert orders.table("orders").current_snapshot().snapshot_id == history[1]


def test_a_rollback_with_a_displaced_commit_is_an_attention_case_but_a_restore_is_not(
    adapter: TrinoIcebergAdapter, orders: LakehouseCatalog, trino: FakeTrino, history: list[int]
) -> None:
    trino.after_rollback = lambda: orders.table("orders").append(row(888))
    trino.before_rollback = lambda: orders.table("orders").append(row(777))
    with pytest.raises(RollbackNeedsAttention) as caught:
        adapter.rollback("orders", history[0], expected_current=history[-1])
    assert caught.value.executed and isinstance(caught.value, BranchDiverged)


def test_a_restored_race_is_not_marked_as_executed(
    adapter: TrinoIcebergAdapter, orders: LakehouseCatalog, trino: FakeTrino, history: list[int]
) -> None:
    trino.before_rollback = lambda: orders.table("orders").append(row(777))
    with pytest.raises(BranchDiverged) as caught:
        adapter.rollback("orders", history[0], expected_current=history[-1])
    assert not getattr(caught.value, "executed", False)  # main is back where the writer left it


def test_a_rollback_the_log_does_not_show_never_moves_main_to_an_older_snapshot(
    adapter: TrinoIcebergAdapter,
    orders: LakehouseCatalog,
    trino: FakeTrino,
    history: list[int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_history = Table.history
    monkeypatch.setattr(Table, "history", lambda self: real_history(self)[:-1])  # drops the entry
    with pytest.raises(RollbackNeedsAttention, match="does not show"):
        adapter.rollback("orders", history[1], expected_current=history[-1])
    assert orders.table("orders").current_snapshot().snapshot_id == history[1]  # not history[0]


def test_an_unlogged_rollback_over_a_writer_is_reported_not_restored_backwards(
    adapter: TrinoIcebergAdapter,
    orders: LakehouseCatalog,
    trino: FakeTrino,
    history: list[int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_history = Table.history
    monkeypatch.setattr(Table, "history", lambda self: real_history(self)[:-1])
    trino.before_rollback = lambda: orders.table("orders").append(row(777))
    with pytest.raises(RollbackNeedsAttention):
        adapter.rollback("orders", history[1], expected_current=history[-1])
    assert orders.table("orders").current_snapshot().snapshot_id == history[1]


def test_a_writer_after_the_rollback_is_left_alone(
    adapter: TrinoIcebergAdapter, orders: LakehouseCatalog, trino: FakeTrino, history: list[int]
) -> None:
    trino.after_rollback = lambda: orders.table("orders").append(row(888))
    with pytest.raises(BranchDiverged, match="left as is"):
        adapter.rollback("orders", history[0], expected_current=history[-1])
    assert 888 in orders.read("orders").column("id").to_pylist()


def test_rollback_through_a_plan(
    adapter: TrinoIcebergAdapter, orders: LakehouseCatalog, trino: FakeTrino, history: list[int]
) -> None:
    action = RollbackToSnapshot("orders", snapshot_id=history[0])
    plan = adapter.plan_maintenance(action)
    assert plan.kind == "rollback" and plan.current_snapshot_id == history[-1]
    result = adapter.apply_maintenance(action, plan)
    assert (result.snapshot_before, result.snapshot_after) == (history[-1], history[0])

    orders.table("orders").append(row(5))  # main moved since the plan
    with pytest.raises(ExecutionError, match="since the plan was made"):
        adapter.apply_maintenance(action, plan)
    assert len(trino.altered()) == 1


# -- maintenance Trino cannot do ------------------------------------------------------------------


@pytest.mark.parametrize(
    "action",
    [
        RemoveOrphanFiles("orders", older_than=REAL_NOW),
        FastForward("orders", to="feature"),
        DropTable("orders"),
        RollbackToSnapshot("orders", ref="feature"),
    ],
)
def test_other_maintenance_is_refused_before_anything_is_sent(
    adapter: TrinoIcebergAdapter, trino: FakeTrino, action: Any
) -> None:
    with pytest.raises(UnsupportedAction):
        adapter.plan_maintenance(action)
    assert trino.statements == []


def test_orphan_cleanup_is_refused_for_want_of_a_dry_run(adapter: TrinoIcebergAdapter) -> None:
    with pytest.raises(UnsupportedAction, match="no dry run"):
        adapter.plan_maintenance(RemoveOrphanFiles("orders", older_than=REAL_NOW))


# -- expire ---------------------------------------------------------------------------------------


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """The adapter believes it is two days from now, so every snapshot of the test is old."""

    class Future(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ANN001, ANN206
            return FUTURE if tz else FUTURE.replace(tzinfo=None)

    monkeypatch.setattr(trino_iceberg, "datetime", Future)


def snapshot_ids(lake: LakehouseCatalog) -> list[int]:
    return [s.snapshot_id for s in lake.snapshots("orders")]


@pytest.mark.usefixtures("clock")
def test_trino_expires_when_it_would_remove_exactly_the_approved_snapshots(
    adapter: TrinoIcebergAdapter, orders: LakehouseCatalog, trino: FakeTrino, history: list[int]
) -> None:
    action = ExpireSnapshots("orders", older_than=CUTOFF)
    plan = adapter.plan_maintenance(action)
    assert [s.snapshot_id for s in plan.expired] == history[:-1]
    assert any("Trino deletes the data" in w for w in plan.warnings)

    result = adapter.apply_maintenance(action, plan)
    assert trino.altered() == [
        'ALTER TABLE "lk"."db"."orders" EXECUTE expire_snapshots(retention_threshold => '
        f"'{int((FUTURE - CUTOFF).total_seconds())}s')"
    ]
    assert set(result.expired_snapshot_ids) == set(history[:-1])
    assert snapshot_ids(orders) == [history[-1]]


@pytest.mark.usefixtures("clock")
def test_explicit_snapshot_ids_go_to_pyiceberg(
    adapter: TrinoIcebergAdapter, orders: LakehouseCatalog, trino: FakeTrino, history: list[int]
) -> None:
    action = ExpireSnapshots("orders", snapshot_ids=(history[0],))
    plan = adapter.plan_maintenance(action)
    assert not any("Trino deletes" in w for w in plan.warnings)
    result = adapter.apply_maintenance(action, plan)
    assert trino.altered() == []
    assert result.expired_snapshot_ids == (history[0],)
    assert snapshot_ids(orders) == history[1:]


@pytest.mark.usefixtures("clock")
def test_keeping_more_than_one_snapshot_goes_to_pyiceberg(
    adapter: TrinoIcebergAdapter, orders: LakehouseCatalog, trino: FakeTrino, history: list[int]
) -> None:
    action = ExpireSnapshots("orders", older_than=CUTOFF, retain_last=2)
    result = adapter.apply_maintenance(action, adapter.plan_maintenance(action))
    assert trino.altered() == []
    assert result.expired_snapshot_ids == (history[0],)
    assert snapshot_ids(orders) == history[1:]


@pytest.mark.usefixtures("clock")
def test_a_snapshot_near_the_cutoff_makes_the_outcome_clock_dependent(
    orders: LakehouseCatalog, trino: FakeTrino, history: list[int]
) -> None:
    near = TrinoIcebergAdapter(orders, trino, "lk", clock_margin=timedelta(days=2))
    action = ExpireSnapshots("orders", older_than=CUTOFF)
    result = near.apply_maintenance(action, near.plan_maintenance(action))
    assert trino.altered() == []  # snapshots lie within the margin of the cutoff
    assert set(result.expired_snapshot_ids) == set(history[:-1])


@pytest.mark.usefixtures("clock")
def test_a_cutoff_in_the_future_is_never_sent_to_trino(
    adapter: TrinoIcebergAdapter, trino: FakeTrino, history: list[int]
) -> None:
    action = ExpireSnapshots("orders", older_than=FUTURE + timedelta(days=1))
    adapter.apply_maintenance(action, adapter.plan_maintenance(action))
    assert trino.altered() == []


@pytest.mark.usefixtures("clock")
def test_a_ref_with_its_own_max_snapshot_age_goes_to_pyiceberg(
    adapter: TrinoIcebergAdapter, orders: LakehouseCatalog, trino: FakeTrino, history: list[int]
) -> None:
    orders.table("orders").manage_snapshots().create_branch(
        history[-1], "aging", max_snapshot_age_ms=1000
    ).commit()
    action = ExpireSnapshots("orders", older_than=CUTOFF)
    adapter.apply_maintenance(action, adapter.plan_maintenance(action))
    assert trino.altered() == []


@pytest.mark.usefixtures("clock")
def test_a_plan_that_changed_since_approval_is_carried_out_by_pyiceberg_at_most(
    adapter: TrinoIcebergAdapter, orders: LakehouseCatalog, trino: FakeTrino, history: list[int]
) -> None:
    action = ExpireSnapshots("orders", older_than=CUTOFF)
    plan = adapter.plan_maintenance(action)
    orders.table("orders").manage_snapshots().create_tag(history[0], "keep-me").commit()
    result = adapter.apply_maintenance(action, plan)
    assert trino.altered() == []
    assert history[0] not in result.expired_snapshot_ids  # the new tag protects it
    assert set(result.expired_snapshot_ids) <= {s.snapshot_id for s in plan.expired}
    assert history[0] in snapshot_ids(orders)


@pytest.mark.usefixtures("clock")
def test_trino_removing_more_than_approved_is_reported(
    adapter: TrinoIcebergAdapter, orders: LakehouseCatalog, trino: FakeTrino, history: list[int]
) -> None:
    action = ExpireSnapshots("orders", older_than=CUTOFF)
    plan = adapter.plan_maintenance(action)
    trino.overreach = True  # a Trino that also takes the snapshot a writer added meanwhile
    trino.before_expire = lambda: orders.table("orders").append(row(5))
    with pytest.raises(ExecutionError, match="outside the approved plan"):
        adapter.apply_maintenance(action, plan)


def tag_with_age(lake: LakehouseCatalog, snapshot: int, name: str = "release") -> None:
    lake.table("orders").manage_snapshots().create_tag(snapshot, name, max_ref_age_ms=1000).commit()


@pytest.mark.usefixtures("clock")
def test_the_fake_models_ref_age_expiry_the_planner_cannot_see(
    orders: LakehouseCatalog, trino: FakeTrino, history: list[int]
) -> None:
    """Guards the regression test below: this Trino drops an aged tag and its snapshot."""
    tag_with_age(orders, history[0])
    plan = TrinoIcebergAdapter(orders, trino, "lk")._meta.plan_maintenance(
        ExpireSnapshots("orders", older_than=CUTOFF)
    )
    assert history[0] not in {s.snapshot_id for s in plan.expired}  # the planner protects it
    trino.cursor().execute(
        'ALTER TABLE "lk"."db"."orders" EXECUTE expire_snapshots(retention_threshold => '
        f"'{int((FUTURE - CUTOFF).total_seconds())}s')"
    )
    assert "release" not in orders.refs("orders") and history[0] not in snapshot_ids(orders)


@pytest.mark.usefixtures("clock")
def test_a_retained_tag_keeps_expiry_away_from_trino(
    adapter: TrinoIcebergAdapter, orders: LakehouseCatalog, trino: FakeTrino, history: list[int]
) -> None:
    tag_with_age(orders, history[0])
    action = ExpireSnapshots("orders", older_than=CUTOFF)
    plan = adapter.plan_maintenance(action)
    assert not any("Trino deletes" in w for w in plan.warnings)
    result = adapter.apply_maintenance(action, plan)
    assert trino.altered() == []
    assert orders.refs("orders")["release"] == history[0]  # the tag and its snapshot survive
    assert set(result.expired_snapshot_ids) == {s.snapshot_id for s in plan.expired}
    assert history[0] in snapshot_ids(orders)


@pytest.mark.usefixtures("clock")
def test_the_table_wide_max_ref_age_keeps_expiry_away_from_trino(
    adapter: TrinoIcebergAdapter, orders: LakehouseCatalog, trino: FakeTrino, history: list[int]
) -> None:
    with orders.table("orders").transaction() as tx:
        tx.set_properties({"history.expire.max-ref-age-ms": "1000"})
    orders.table("orders").manage_snapshots().create_branch(history[0], "side").commit()
    action = ExpireSnapshots("orders", older_than=CUTOFF)
    adapter.apply_maintenance(action, adapter.plan_maintenance(action))
    assert trino.altered() == []
    assert orders.refs("orders")["side"] == history[0]


@pytest.mark.usefixtures("clock")
@pytest.mark.parametrize("ref_kind", ["branch", "tag"])
def test_any_ref_besides_main_keeps_expiry_away_from_trino(
    adapter: TrinoIcebergAdapter,
    orders: LakehouseCatalog,
    trino: FakeTrino,
    history: list[int],
    ref_kind: str,
) -> None:
    manage = orders.table("orders").manage_snapshots()
    (manage.create_branch if ref_kind == "branch" else manage.create_tag)(
        history[-1], "other"
    ).commit()
    action = ExpireSnapshots("orders", older_than=CUTOFF)
    adapter.apply_maintenance(action, adapter.plan_maintenance(action))
    assert trino.altered() == []


@pytest.mark.usefixtures("clock")
def test_a_history_expire_table_property_keeps_expiry_away_from_trino(
    adapter: TrinoIcebergAdapter, orders: LakehouseCatalog, trino: FakeTrino, history: list[int]
) -> None:
    with orders.table("orders").transaction() as tx:
        tx.set_properties({"history.expire.max-snapshot-age-ms": "1000"})
    action = ExpireSnapshots("orders", older_than=CUTOFF)
    adapter.apply_maintenance(action, adapter.plan_maintenance(action))
    assert trino.altered() == []


@pytest.mark.usefixtures("clock")
def test_main_with_its_own_retention_keeps_expiry_away_from_trino(
    adapter: TrinoIcebergAdapter, orders: LakehouseCatalog, trino: FakeTrino, history: list[int]
) -> None:
    table = orders.table("orders")
    with table.transaction() as tx:
        tx._apply(
            (
                SetSnapshotRefUpdate(
                    ref_name="main",
                    type="branch",
                    snapshot_id=history[-1],
                    min_snapshots_to_keep=2,
                ),
            )
        )
    action = ExpireSnapshots("orders", older_than=CUTOFF)
    adapter.apply_maintenance(action, adapter.plan_maintenance(action))
    assert trino.altered() == []


@pytest.mark.usefixtures("clock")
def test_a_ref_that_trino_drops_is_reported_even_if_no_snapshot_goes(
    adapter: TrinoIcebergAdapter,
    orders: LakehouseCatalog,
    trino: FakeTrino,
    history: list[int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defence in depth: force the Trino path past the guard on refs and let Trino drop a tag."""
    tag_with_age(orders, history[-1], "late")  # on main's head: only the tag goes, no snapshot
    action = ExpireSnapshots("orders", older_than=CUTOFF)
    plan = adapter.plan_maintenance(action)
    monkeypatch.setattr(adapter, "_trino_may_expire", lambda _action: True)
    plan = replace(plan, warnings=(*plan.warnings, trino_iceberg._WARNING_EXPIRE))
    with pytest.raises(MaintenanceNeedsAttention, match=r"ref\(s\) \['late'\]") as caught:
        adapter.apply_maintenance(action, plan)
    assert caught.value.executed  # it ran: the Guard must not record "nothing happened"
    assert isinstance(caught.value, ExecutionError)
    assert trino.altered() and "late" not in orders.refs("orders")


def test_ordinary_commits_to_refs_are_not_alarms_but_removal_and_repointing_are(
    adapter: TrinoIcebergAdapter, orders: LakehouseCatalog, history: list[int]
) -> None:
    orders.table("orders").manage_snapshots().create_branch(history[1], "feature").commit()
    orders.table("orders").manage_snapshots().create_tag(history[0], "v1").commit()
    before = adapter._ref_state(orders.table("orders"))

    orders.table("orders").append(row(10), branch="feature")  # advances on its own history
    orders.table("orders").append(row(11))  # main advances too
    adapter._check_refs("orders", before, ignore_main=False)  # no alarm

    orders.table("orders").manage_snapshots().remove_tag("v1").commit()
    with pytest.raises(MaintenanceNeedsAttention, match="v1"):
        adapter._check_refs("orders", before, ignore_main=False)
    orders.table("orders").manage_snapshots().create_branch(history[0], "v1").commit()  # kind
    with pytest.raises(MaintenanceNeedsAttention, match="v1"):
        adapter._check_refs("orders", before, ignore_main=False)


@pytest.mark.usefixtures("clock")
def test_a_ref_moved_off_its_history_during_a_trino_run_is_an_alarm(
    adapter: TrinoIcebergAdapter,
    orders: LakehouseCatalog,
    trino: FakeTrino,
    history: list[int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orders.table("orders").manage_snapshots().create_branch(history[-1], "feature").commit()
    side = history[0]
    action = ExpireSnapshots("orders", older_than=CUTOFF)
    plan = adapter.plan_maintenance(action)
    monkeypatch.setattr(adapter, "_trino_may_expire", lambda _action: True)
    plan = replace(plan, warnings=(*plan.warnings, trino_iceberg._WARNING_EXPIRE))

    def repoint() -> None:  # `feature` is re-created on an unrelated (older) snapshot
        manage = orders.table("orders").manage_snapshots()
        manage.remove_branch("feature").commit()
        orders.table("orders").manage_snapshots().create_branch(side, "feature").commit()

    trino.before_expire = repoint
    with pytest.raises(MaintenanceNeedsAttention, match="feature"):
        adapter.apply_maintenance(action, plan)


@pytest.mark.usefixtures("clock")
def test_the_trino_path_needs_the_warning_the_approver_saw(
    adapter: TrinoIcebergAdapter, orders: LakehouseCatalog, trino: FakeTrino, history: list[int]
) -> None:
    orders.table("orders").manage_snapshots().create_tag(history[-1], "temp").commit()
    action = ExpireSnapshots("orders", older_than=CUTOFF)
    plan = adapter.plan_maintenance(action)  # planned while a ref made Trino ineligible
    assert not plan.warnings
    orders.table("orders").manage_snapshots().remove_tag("temp").commit()
    adapter.apply_maintenance(action, plan)  # Trino would be eligible now, but was not approved
    assert trino.altered() == []


def test_the_warning_is_given_whenever_trino_may_run_it(
    adapter: TrinoIcebergAdapter, history: list[int]
) -> None:
    # The cutoff is still in the future, so right now it would go to PyIceberg; the clock moving
    # past it must not turn an unwarned plan into a Trino run.
    plan = adapter.plan_maintenance(ExpireSnapshots("orders", older_than=FUTURE))
    assert any("Trino deletes" in w for w in plan.warnings)


@pytest.mark.usefixtures("clock")
def test_trinos_own_retention_floor_falls_back_to_pyiceberg(
    adapter: TrinoIcebergAdapter, orders: LakehouseCatalog, trino: FakeTrino, history: list[int]
) -> None:
    trino.fail_on = (
        "expire_snapshots",
        "Retention specified (2.00d) is shorter than the minimum retention configured in the "
        "system (7.00d)",
    )
    action = ExpireSnapshots("orders", older_than=CUTOFF)
    result = adapter.apply_maintenance(action, adapter.plan_maintenance(action))
    assert set(result.expired_snapshot_ids) == set(history[:-1])
    assert snapshot_ids(orders) == [history[-1]]


@pytest.mark.usefixtures("clock")
def test_other_trino_expire_failures_are_not_hidden(
    adapter: TrinoIcebergAdapter, orders: LakehouseCatalog, trino: FakeTrino, history: list[int]
) -> None:
    trino.fail_on = ("expire_snapshots", "storage unavailable")
    action = ExpireSnapshots("orders", older_than=CUTOFF)
    with pytest.raises(ExecutionError, match="storage unavailable"):
        adapter.apply_maintenance(action, adapter.plan_maintenance(action))
    assert snapshot_ids(orders) == history


# -- construction ---------------------------------------------------------------------------------


def test_the_catalog_name_is_validated(orders: LakehouseCatalog, trino: FakeTrino) -> None:
    with pytest.raises(ExecutionError):
        TrinoIcebergAdapter(orders, trino, 'lk"; DROP')


def test_connect_needs_the_optional_trino_package(
    orders: LakehouseCatalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "trino.dbapi", None)
    with pytest.raises(ExecutionError, match="pip install trino"):
        TrinoIcebergAdapter.connect(orders, host="localhost")
