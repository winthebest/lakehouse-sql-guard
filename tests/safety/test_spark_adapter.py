"""SparkIcebergAdapter: SQL building and control flow without Spark, then the real stack.

The unit tests drive the adapter with a recording stand-in for the Spark session; effects Spark
would have on the table (creating a branch, moving main) are reproduced with PyIceberg so the
adapter's checks run against real table metadata. The `spark` tests need the Docker stack in
`docker/` and are deselected by default (`pytest -m spark`).
"""

from __future__ import annotations

import os
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pyarrow as pa
import pytest
from sqlglot import parse_one

from sqlguard.core.catalog import LakehouseCatalog
from sqlguard.safety.actions import (
    ExpireSnapshots,
    FastForward,
    RemoveOrphanFiles,
    RollbackToSnapshot,
)
from sqlguard.safety.executor import (
    BranchDiverged,
    BranchHandle,
    ExecutionError,
    SparkIcebergAdapter,
    UnsupportedAction,
)
from sqlguard.safety.executor.spark_iceberg import (
    branch_identifier,
    procedure_call,
    spark_statement,
    table_identifier,
    to_arrow,
)
from sqlguard.safety.guard import Guard, Outcome
from sqlguard.safety.policy import Policy, TaskScope

# -- pure SQL building ---------------------------------------------------------------------------


def test_procedure_call_renders_named_arguments() -> None:
    cutoff = datetime(2026, 10, 1, 12, 30, tzinfo=UTC)
    assert procedure_call(
        "demo", "fast_forward", table="db.orders", branch="main", to="audit-1"
    ) == (
        "CALL `demo`.system.fast_forward(table => 'db.orders', branch => 'main', to => 'audit-1')"
    )
    assert procedure_call(
        "demo",
        "expire_snapshots",
        table="db.orders",
        older_than=cutoff,
        retain_last=None,
        snapshot_ids=[1, 22],
        dry_run=False,
    ) == (
        "CALL `demo`.system.expire_snapshots(table => 'db.orders', "
        "older_than => TIMESTAMP '2026-10-01 12:30:00.000000+00:00', "
        "snapshot_ids => array(1, 22), dry_run => false)"
    )


@pytest.mark.parametrize("value", ["db.o'rders", "x'); DROP", "a b", "a\\b", "a`b", ""])
def test_procedure_call_refuses_unsafe_strings(value: str) -> None:
    with pytest.raises(ExecutionError):
        procedure_call("demo", "rollback_to_snapshot", table=value)


def test_identifiers_are_quoted_per_part_and_reject_odd_names() -> None:
    assert table_identifier("demo", "db.orders") == "`demo`.`db`.`orders`"
    assert branch_identifier("demo", "db.orders", "sqlguard-ab12") == (
        "`demo`.`db`.`orders`.`branch_sqlguard-ab12`"
    )
    for bad in ("orders", "db.or`ders", "db.or ders", "db.x;y"):
        with pytest.raises(ExecutionError):
            table_identifier("demo", bad)
    with pytest.raises(ExecutionError):
        branch_identifier("demo", "db.orders", "bad branch")


BRANCH = BranchHandle("db.orders", "sqlguard-ab12", 1)
BRANCH_ID = "`demo`.`db`.`orders`.`branch_sqlguard-ab12`"


def render(sql: str, readable: tuple[str, ...] = (), target: BranchHandle | None = BRANCH) -> str:
    return spark_statement(
        parse_one(sql, dialect="duckdb"),
        spark_catalog="demo",
        default_namespace="db",
        readable=readable,
        target=target,
    )


def test_dml_is_pinned_to_the_branch() -> None:
    assert render("DELETE FROM orders WHERE id = 7") == f"DELETE FROM {BRANCH_ID} WHERE id = 7"
    assert render("UPDATE db.orders SET amount = 0 WHERE id = 7") == (
        f"UPDATE {BRANCH_ID} SET amount = 0 WHERE id = 7"
    )
    assert render("INSERT INTO orders (id, customer, amount) VALUES (1, 'a', 2.0)") == (
        f"INSERT INTO {BRANCH_ID} (id, customer, amount) VALUES (1, 'a', 2.0)"
    )


def test_every_reference_to_the_target_reads_the_branch_and_others_read_main() -> None:
    sql = render(
        "DELETE FROM orders WHERE id IN (SELECT o.id FROM orders o JOIN customers c "
        "ON c.id = o.customer_id WHERE c.country = 'VN')",
        readable=("db.customers",),
    )
    assert sql.count(BRANCH_ID) == 2
    assert "`demo`.`db`.`customers`" in sql
    assert "branch_" not in sql.split("JOIN")[1]


def test_ctes_are_not_qualified() -> None:
    sql = render(
        "DELETE FROM orders WHERE id IN (WITH stale AS (SELECT id FROM orders WHERE amount < 1) "
        "SELECT id FROM stale)"
    )
    assert "FROM stale" in sql
    assert "`demo`.`db`.`stale`" not in sql


def test_a_statement_cannot_write_anything_but_the_branch_table() -> None:
    with pytest.raises(UnsupportedAction):
        render("DELETE FROM customers WHERE id = 1", readable=("db.customers",))
    with pytest.raises(UnsupportedAction):
        render("DELETE FROM other.orders WHERE id = 1")
    with pytest.raises(UnsupportedAction):
        render("DELETE FROM orders WHERE id = 1", target=None)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM secrets",
        "DELETE FROM orders WHERE id IN (SELECT id FROM audit.secrets)",
        "SELECT * FROM other_catalog.db.orders",
    ],
)
def test_tables_that_were_not_cleared_are_refused(sql: str) -> None:
    with pytest.raises(ExecutionError):
        render(sql)


def test_reads_are_qualified_and_the_reserved_token_is_refused() -> None:
    sql = render("SELECT o.id FROM orders o WHERE o.id > 3", readable=("db.orders",), target=None)
    assert sql == "SELECT o.id FROM `demo`.`db`.`orders` AS o WHERE o.id > 3"
    with pytest.raises(ExecutionError):
        render("SELECT 1 FROM __sqlguard_branch_target__", target=None)


# -- Spark results to Arrow ----------------------------------------------------------------------


class FakeFrame:
    def __init__(self, rows: list[tuple] | None = None, columns: list[tuple[str, str]] = ()):
        self._rows = rows or []
        self.schema = SimpleNamespace(
            fields=[
                SimpleNamespace(name=n, dataType=SimpleNamespace(simpleString=lambda t=t: t))
                for n, t in columns
            ]
        )

    def collect(self) -> list[tuple]:
        return self._rows


def test_to_arrow_keeps_types_with_and_without_rows() -> None:
    columns = [
        ("id", "bigint"),
        ("name", "string"),
        ("price", "decimal(10,2)"),
        ("ts", "timestamp"),
    ]
    moment = datetime(2026, 1, 1, 12, tzinfo=UTC).astimezone().replace(tzinfo=None)
    table = to_arrow(FakeFrame([(1, "a", None, moment)], columns))
    assert table.schema.types[0] == pa.int64()
    assert table.schema.types[2] == pa.decimal128(10, 2)
    assert table.column("ts").to_pylist() == [datetime(2026, 1, 1, 12, tzinfo=UTC)]
    empty = to_arrow(FakeFrame([], columns))
    assert empty.num_rows == 0 and empty.schema.types[1] == pa.string()


# -- adapter control flow with a recording session -----------------------------------------------


class FakeSpark:
    """Records statements; `effects` maps a substring of the SQL to a function of the SQL."""

    def __init__(self) -> None:
        self.statements: list[str] = []
        self.effects: list[tuple[str, Any]] = []
        self.fail_on: str | None = None
        self.columns: list[tuple[str, str]] = []

    def on(self, needle: str, effect: Any) -> None:
        self.effects.append((needle, effect))

    def sql(self, text: str) -> FakeFrame:
        self.statements.append(text)
        if self.fail_on and self.fail_on in text:
            raise RuntimeError("boom " + "x" * 2000)
        for needle, effect in self.effects:
            if needle in text:
                return FakeFrame(effect(text) or [], self.columns)
        return FakeFrame()

    def calls(self, needle: str) -> list[str]:
        return [s for s in self.statements if needle in s]


@pytest.fixture
def lake(tmp_path: Path) -> LakehouseCatalog:
    catalog = LakehouseCatalog.local(tmp_path / "warehouse")
    catalog.create_table(
        "orders",
        pa.table(
            {
                "id": pa.array(range(1, 21), pa.int64()),
                "customer": pa.array([f"c{i % 4}" for i in range(20)]),
                "amount": pa.array([float(i) for i in range(20)]),
            }
        ),
    )
    return catalog


@pytest.fixture
def spark() -> FakeSpark:
    return FakeSpark()


@pytest.fixture
def adapter(lake: LakehouseCatalog, spark: FakeSpark) -> SparkIcebergAdapter:
    def create_branch(text: str):
        name = re.search(r"CREATE BRANCH `([^`]+)` AS OF VERSION (\d+)", text)
        assert name
        lake.table("orders").manage_snapshots().create_branch(
            int(name.group(2)), name.group(1)
        ).commit()

    spark.on("CREATE BRANCH", create_branch)
    return SparkIcebergAdapter(lake, spark, "demo")


def commit_to_branch(lake: LakehouseCatalog, branch: BranchHandle, keep_below: int) -> int:
    """What the DELETE would have done on the branch, written with PyIceberg."""
    iceberg = lake.table("orders")
    remaining = lake.read("orders", branch.base_snapshot_id).filter(
        pa.compute.less(lake.read("orders", branch.base_snapshot_id)["id"], keep_below)
    )
    iceberg.overwrite(remaining, branch=branch.name)
    return lake.table("orders").refs()[branch.name].snapshot_id


def publish(adapter: SparkIcebergAdapter, branch: BranchHandle):
    """Publish the head the diff recorded, the way the Guard does."""
    recorded = adapter.diff(branch).branch
    return adapter.publish(recorded, recorded.head)


def test_branch_lifecycle_and_publish_by_snapshot_id(
    adapter: SparkIcebergAdapter, lake: LakehouseCatalog, spark: FakeSpark
) -> None:
    base = adapter.current_snapshot_id("orders")
    branch = adapter.create_branch("orders")
    assert branch.table == "db.orders" and branch.base_snapshot_id == base
    assert branch.name.startswith("sqlguard-")
    assert spark.calls("CREATE BRANCH")[0].startswith("ALTER TABLE `demo`.`db`.`orders` CREATE")

    adapter.apply_sql(branch, "DELETE FROM orders WHERE id >= 20")
    assert spark.statements[-1] == (
        f"DELETE FROM `demo`.`db`.`orders`.`branch_{branch.name}` WHERE id >= 20"
    )
    head = commit_to_branch(lake, branch, keep_below=20)
    assert adapter.current_snapshot_id("orders") == base  # branch writes never touch main

    diff = adapter.diff(branch, key=("id",))
    assert diff.impact.rows_deleted == 1

    published = publish(adapter, branch)
    assert published.changed and published.previous_snapshot_id == base
    assert published.snapshot_id == head == adapter.current_snapshot_id("orders")
    assert not spark.calls("fast_forward")  # PyIceberg moved main, not a Spark procedure

    adapter.drop_branch(branch)
    assert spark.statements[-1] == (
        f"ALTER TABLE `demo`.`db`.`orders` DROP BRANCH IF EXISTS `{branch.name}`"
    )


def test_publish_without_changes_does_not_call_spark(
    adapter: SparkIcebergAdapter, spark: FakeSpark
) -> None:
    branch = adapter.create_branch("orders")
    result = publish(adapter, branch)
    assert not result.changed
    assert not spark.calls("fast_forward")


def test_publish_refuses_when_main_moved(
    adapter: SparkIcebergAdapter, lake: LakehouseCatalog, spark: FakeSpark
) -> None:
    branch = adapter.create_branch("orders")
    commit_to_branch(lake, branch, keep_below=20)
    lake.table("orders").append(
        pa.table({"id": pa.array([99], pa.int64()), "customer": ["z"], "amount": pa.array([1.0])})
    )
    with pytest.raises(BranchDiverged):
        publish(adapter, branch)
    assert not spark.calls("fast_forward")


def test_a_writer_committing_during_publish_is_reported_as_divergence(
    adapter: SparkIcebergAdapter,
    lake: LakehouseCatalog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pyiceberg.table.update.snapshot import ManageSnapshots

    branch = adapter.create_branch("orders")
    commit_to_branch(lake, branch, keep_below=20)
    recorded = adapter.diff(branch).branch
    original = ManageSnapshots.commit

    def writer_wins(self):
        lake.table("orders").append(
            pa.table(
                {"id": pa.array([99], pa.int64()), "customer": ["z"], "amount": pa.array([1.0])}
            )
        )
        return original(self)

    monkeypatch.setattr(ManageSnapshots, "commit", writer_wins)
    with pytest.raises(BranchDiverged):
        adapter.publish(recorded, recorded.head)


def test_spark_errors_become_execution_errors_with_bounded_text(
    adapter: SparkIcebergAdapter, spark: FakeSpark
) -> None:
    branch = adapter.create_branch("orders")
    spark.fail_on = "DELETE"
    with pytest.raises(ExecutionError) as caught:
        adapter.apply_sql(branch, "DELETE FROM orders WHERE id = 1")
    assert len(str(caught.value)) < 700


def test_apply_sql_refuses_other_tables_and_main(adapter: SparkIcebergAdapter) -> None:
    branch = adapter.create_branch("orders")
    with pytest.raises(UnsupportedAction):
        adapter.apply_sql(branch, "DROP TABLE orders")
    with pytest.raises(ExecutionError):
        adapter.apply_sql(branch, "DELETE FROM orders WHERE id IN (SELECT id FROM secrets)")
    with pytest.raises(ExecutionError):
        adapter.drop_branch(BranchHandle("db.orders", "main", 1))
    with pytest.raises(ExecutionError):
        adapter.create_branch("orders", "main")


def test_query_runs_read_only_sql_on_main(adapter: SparkIcebergAdapter, spark: FakeSpark) -> None:
    spark.on("COUNT(*)", lambda _: [(20,)])
    spark.columns = [("n", "bigint")]
    result = adapter.query("SELECT count(*) AS n FROM orders", ["orders"])
    assert result.to_pylist() == [{"n": 20}]
    assert spark.statements[-1] == "SELECT COUNT(*) AS n FROM `demo`.`db`.`orders`"
    with pytest.raises(UnsupportedAction):
        adapter.query("DELETE FROM orders", ["orders"])
    with pytest.raises(ExecutionError):
        adapter.query("SELECT * FROM nowhere", ["nowhere"])


# -- maintenance ---------------------------------------------------------------------------------


def add_snapshots(lake: LakehouseCatalog, count: int) -> None:
    for i in range(count):
        lake.table("orders").append(
            pa.table(
                {
                    "id": pa.array([100 + i], pa.int64()),
                    "customer": ["n"],
                    "amount": pa.array([1.0]),
                }
            )
        )


def test_expire_is_planned_with_a_warning_and_applied_by_snapshot_id(
    adapter: SparkIcebergAdapter, lake: LakehouseCatalog, spark: FakeSpark
) -> None:
    add_snapshots(lake, 3)
    ids = [s.snapshot_id for s in lake.snapshots("orders")]
    action = ExpireSnapshots("orders", snapshot_ids=(ids[0], ids[1]))
    plan = adapter.plan_maintenance(action)
    assert [s.snapshot_id for s in plan.expired] == [ids[0], ids[1]]
    assert plan.irreversible and any("deletes the data" in w for w in plan.warnings)
    assert not spark.calls("expire_snapshots")  # planning touches nothing

    result = adapter.apply_maintenance(action, plan)
    assert result.expired_snapshot_ids == (ids[0], ids[1])
    assert spark.calls("expire_snapshots") == [
        f"CALL `demo`.system.expire_snapshots(table => 'db.orders', "
        f"snapshot_ids => array({ids[0]}, {ids[1]}))"
    ]


def test_expire_never_passes_a_ref_head_to_spark(
    adapter: SparkIcebergAdapter, lake: LakehouseCatalog, spark: FakeSpark
) -> None:
    add_snapshots(lake, 2)
    ids = [s.snapshot_id for s in lake.snapshots("orders")]
    action = ExpireSnapshots("orders", snapshot_ids=(ids[0],))
    plan = adapter.plan_maintenance(action)
    lake.table("orders").manage_snapshots().create_tag(ids[0], "keep").commit()
    adapter.apply_maintenance(action, plan)
    assert not spark.calls("expire_snapshots")


def test_rollback_uses_the_procedure(
    adapter: SparkIcebergAdapter, lake: LakehouseCatalog, spark: FakeSpark
) -> None:
    add_snapshots(lake, 1)
    first = lake.snapshots("orders")[0].snapshot_id
    spark.on(
        "rollback_to_snapshot",
        lambda text: lake.table("orders").manage_snapshots().rollback_to_snapshot(first).commit(),
    )
    action = RollbackToSnapshot("orders", snapshot_id=first)
    plan = adapter.plan_maintenance(action)
    assert not plan.irreversible
    result = adapter.apply_maintenance(action, plan)
    assert result.snapshot_after == first
    assert spark.calls("rollback_to_snapshot") == [
        f"CALL `demo`.system.rollback_to_snapshot(table => 'db.orders', snapshot_id => {first})"
    ]


def test_an_approved_fast_forward_moves_main_to_the_planned_snapshot_without_spark(
    adapter: SparkIcebergAdapter, lake: LakehouseCatalog, spark: FakeSpark
) -> None:
    base = adapter.current_snapshot_id("orders")
    lake.table("orders").manage_snapshots().create_branch(base, "release").commit()
    lake.table("orders").overwrite(lake.read("orders").slice(0, 3), branch="release")
    planned = lake.table("orders").refs()["release"].snapshot_id
    action = FastForward("orders", to="release")
    plan = adapter.plan_maintenance(action)
    assert plan.target_snapshot_id == planned
    # the branch keeps receiving writes after the plan was approved
    lake.table("orders").append(lake.read("orders").slice(0, 1), branch="release")
    result = adapter.apply_maintenance(action, plan)
    assert result.snapshot_after == planned == adapter.current_snapshot_id("orders")
    assert not spark.calls("fast_forward")


def test_apply_refuses_a_stale_plan(
    adapter: SparkIcebergAdapter, lake: LakehouseCatalog, spark: FakeSpark
) -> None:
    add_snapshots(lake, 1)
    first = lake.snapshots("orders")[0].snapshot_id
    action = RollbackToSnapshot("orders", snapshot_id=first)
    plan = adapter.plan_maintenance(action)
    add_snapshots(lake, 1)
    with pytest.raises(ExecutionError, match="since the plan was made"):
        adapter.apply_maintenance(action, plan)
    assert not spark.calls("rollback")


def test_fast_forward_refuses_audit_branches(adapter: SparkIcebergAdapter) -> None:
    branch = adapter.create_branch("orders")
    with pytest.raises(ExecutionError, match="internal audit branch"):
        adapter.plan_maintenance(FastForward("orders", to=branch.name))


def test_rollback_restores_a_writer_that_slipped_in(
    adapter: SparkIcebergAdapter, lake: LakehouseCatalog, spark: FakeSpark
) -> None:
    add_snapshots(lake, 1)
    first, expected = (s.snapshot_id for s in lake.snapshots("orders"))
    newer = 12345
    spark.on("rollback_to_snapshot", lambda text: [(newer, first)])
    with pytest.raises(BranchDiverged, match="restored"):
        adapter.rollback("orders", first, expected_current=expected)
    assert spark.calls("set_current_snapshot") == [
        f"CALL `demo`.system.set_current_snapshot(table => 'db.orders', snapshot_id => {newer})"
    ]
    with pytest.raises(BranchDiverged):
        adapter.rollback("orders", first, expected_current=first)


def orphan_location(lake: LakehouseCatalog, name: str = "orphan.parquet") -> str:
    location = lake.table("orders").location().removeprefix("file://")
    path = Path(location) / "data" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * 10)
    return f"file://{path}"


def test_orphan_plan_comes_from_the_dry_run_and_apply_stays_inside_it(
    adapter: SparkIcebergAdapter, lake: LakehouseCatalog, spark: FakeSpark
) -> None:
    orphan = orphan_location(lake)
    spark.on("dry_run => true", lambda _: [(orphan,)])
    spark.on("dry_run => false", lambda _: [(orphan,)])
    cutoff = datetime.now(UTC) - timedelta(days=5)
    action = RemoveOrphanFiles("orders", older_than=cutoff)

    plan = adapter.plan_maintenance(action)
    assert [f.location for f in plan.orphan_files] == [orphan]
    assert plan.orphan_files[0].size == 10 and plan.irreversible
    assert not spark.calls("dry_run => false")

    result = adapter.apply_maintenance(action, plan)
    assert result.deleted_files == (orphan,)
    assert "dry_run => false" in spark.statements[-1]


def test_orphan_apply_refuses_files_the_plan_did_not_list(
    adapter: SparkIcebergAdapter, lake: LakehouseCatalog, spark: FakeSpark
) -> None:
    approved = orphan_location(lake, "a.parquet")
    extra = orphan_location(lake, "b.parquet")
    answer = [(approved,)]
    spark.on("dry_run => true", lambda _: answer)
    action = RemoveOrphanFiles("orders", older_than=datetime.now(UTC) - timedelta(days=5))
    plan = adapter.plan_maintenance(action)
    answer.append((extra,))
    with pytest.raises(ExecutionError, match="not in the approved plan"):
        adapter.apply_maintenance(action, plan)
    assert not spark.calls("dry_run => false")


def test_orphan_plan_refuses_a_file_the_table_still_references(
    adapter: SparkIcebergAdapter, lake: LakehouseCatalog, spark: FakeSpark
) -> None:
    live = lake.table("orders").current_snapshot().manifest_list
    spark.on("dry_run => true", lambda _: [(live,)])
    action = RemoveOrphanFiles("orders", older_than=datetime.now(UTC) - timedelta(days=5))
    with pytest.raises(ExecutionError, match="referenced file"):
        adapter.plan_maintenance(action)


def test_orphan_plan_refuses_files_outside_the_table_location(
    adapter: SparkIcebergAdapter, spark: FakeSpark, tmp_path: Path
) -> None:
    stray = tmp_path / "elsewhere" / "x.parquet"
    stray.parent.mkdir()
    stray.write_bytes(b"x")
    spark.on("dry_run => true", lambda _: [(f"file://{stray}",)])
    action = RemoveOrphanFiles("orders", older_than=datetime.now(UTC) - timedelta(days=5))
    with pytest.raises(ExecutionError, match="outside the table location"):
        adapter.plan_maintenance(action)


# -- the Guard on top of the adapter -------------------------------------------------------------


def test_guard_publishes_a_small_delete_through_the_spark_adapter(
    adapter: SparkIcebergAdapter, lake: LakehouseCatalog, spark: FakeSpark, tmp_path: Path
) -> None:
    """The Guard's ordinary pipeline works unchanged on this adapter."""

    def apply_delete(text: str):
        branch = re.search(r"branch_([\w-]+)`", text).group(1)
        handle = BranchHandle("db.orders", branch, lake.table("orders").refs()[branch].snapshot_id)
        commit_to_branch(lake, handle, keep_below=20)

    spark.on("DELETE FROM", apply_delete)
    guard = Guard(lake, Policy(), audit_path=tmp_path / "audit.jsonl", adapter=adapter)
    result = guard.submit(_sql("DELETE FROM orders WHERE id = 20"), TaskScope.of(namespaces=["db"]))
    assert result.outcome is Outcome.PUBLISHED, result.reasons
    assert sorted(lake.read("orders").column("id").to_pylist()) == list(range(1, 20))
    assert not spark.calls("fast_forward")


def _sql(text: str):
    from sqlguard.safety.actions import SqlAction

    return SqlAction(text)


# -- real Spark (docker compose stack) -----------------------------------------------------------

REMOTE = os.environ.get("SQLGUARD_SPARK_REMOTE", "sc://localhost:15002")
REST_URI = os.environ.get("SQLGUARD_REST_URI", "http://localhost:8181")
S3_ENDPOINT = os.environ.get("SQLGUARD_S3_ENDPOINT", "http://localhost:9000")
S3_KEY = os.environ.get("SQLGUARD_S3_KEY", "admin")
S3_SECRET = os.environ.get("SQLGUARD_S3_SECRET", "password")
SPARK_CATALOG = os.environ.get("SQLGUARD_SPARK_CATALOG", "demo")


@pytest.fixture(scope="module")
def spark_session():
    pyspark_sql = pytest.importorskip("pyspark.sql", reason="install the `spark` extra")
    try:
        session = pyspark_sql.SparkSession.builder.remote(REMOTE).getOrCreate()
        session.sql("SELECT 1").collect()
    except Exception as err:  # noqa: BLE001 - any connection failure means the stack is down
        pytest.skip(f"no Spark Connect server at {REMOTE}: {err}")
    yield session
    session.stop()


@pytest.fixture
def real_lake(spark_session):
    from pyiceberg.catalog.rest import RestCatalog

    namespace = f"t{uuid.uuid4().hex[:8]}"
    rest = RestCatalog(
        "demo",
        uri=REST_URI,
        **{
            "s3.endpoint": S3_ENDPOINT,
            "s3.access-key-id": S3_KEY,
            "s3.secret-access-key": S3_SECRET,
            "s3.region": "us-east-1",
            "s3.path-style-access": "true",
        },
    )
    rest.create_namespace(namespace)
    lake = LakehouseCatalog(rest, namespace)
    spark_session.sql("SET spark.sql.session.timeZone = UTC")
    yield lake
    for identifier in rest.list_tables(namespace):
        rest.purge_table(identifier)
    rest.drop_namespace(namespace)


@pytest.fixture
def real_adapter(real_lake: LakehouseCatalog, spark_session) -> SparkIcebergAdapter:
    real_lake.create_table(
        "orders",
        pa.table(
            {
                "id": pa.array(range(1, 21), pa.int64()),
                "customer": pa.array([f"c{i % 4}" for i in range(20)]),
                "amount": pa.array([float(i) for i in range(20)]),
            }
        ),
    )
    return SparkIcebergAdapter(real_lake, spark_session, SPARK_CATALOG)


@pytest.mark.spark
def test_spark_branch_write_and_publish_by_snapshot(
    real_adapter: SparkIcebergAdapter, real_lake: LakehouseCatalog
) -> None:
    base = real_adapter.current_snapshot_id("orders")
    branch = real_adapter.create_branch("orders")
    real_adapter.apply_sql(branch, "DELETE FROM orders WHERE id > 15")
    assert real_adapter.current_snapshot_id("orders") == base
    diff = real_adapter.diff(branch, key=("id",))
    assert diff.impact.rows_deleted == 5

    published = publish(real_adapter, branch)
    assert published.changed
    assert sorted(real_lake.read("orders").column("id").to_pylist()) == list(range(1, 16))
    real_adapter.drop_branch(branch)
    assert branch.name not in real_lake.table("orders").refs()


@pytest.mark.spark
def test_spark_publish_refuses_when_main_moved(
    real_adapter: SparkIcebergAdapter, real_lake: LakehouseCatalog
) -> None:
    branch = real_adapter.create_branch("orders")
    real_adapter.apply_sql(branch, "DELETE FROM orders WHERE id > 15")
    other = real_adapter.create_branch("orders", "mover")
    real_adapter.apply_sql(other, "DELETE FROM orders WHERE id = 2")
    publish(real_adapter, other)
    with pytest.raises(BranchDiverged):
        publish(real_adapter, branch)


@pytest.mark.spark
def test_spark_query_returns_arrow(real_adapter: SparkIcebergAdapter) -> None:
    result = real_adapter.query(
        "SELECT customer, count(*) AS n FROM orders GROUP BY customer ORDER BY customer",
        ["orders"],
    )
    assert result.column("n").to_pylist() == [5, 5, 5, 5]
    assert result.schema.field("n").type == pa.int64()


@pytest.mark.spark
def test_spark_rollback_and_snapshot_expiry(
    real_adapter: SparkIcebergAdapter, real_lake: LakehouseCatalog
) -> None:
    first = real_adapter.current_snapshot_id("orders")
    branch = real_adapter.create_branch("orders")
    real_adapter.apply_sql(branch, "DELETE FROM orders WHERE id > 15")
    publish(real_adapter, branch)
    real_adapter.drop_branch(branch)
    second = real_adapter.current_snapshot_id("orders")

    action = RollbackToSnapshot("orders", snapshot_id=first)
    real_adapter.apply_maintenance(action, real_adapter.plan_maintenance(action))
    assert real_adapter.current_snapshot_id("orders") == first
    assert real_lake.read("orders").num_rows == 20

    expire = ExpireSnapshots("orders", snapshot_ids=(second,))
    plan = real_adapter.plan_maintenance(expire)
    result = real_adapter.apply_maintenance(expire, plan)
    assert result.expired_snapshot_ids == (second,)
    remaining = [s.snapshot_id for s in real_lake.snapshots("orders")]
    assert second not in remaining and first in remaining
    assert real_lake.read("orders").num_rows == 20  # expiry did not touch live data


@pytest.mark.spark
def test_spark_orphan_files_are_planned_with_a_dry_run(
    real_adapter: SparkIcebergAdapter, real_lake: LakehouseCatalog
) -> None:
    action = RemoveOrphanFiles("orders", older_than=datetime.now(UTC) - timedelta(days=2))
    plan = real_adapter.plan_maintenance(action)
    assert plan.irreversible
    assert plan.orphan_files == ()
    result = real_adapter.apply_maintenance(action, plan)
    assert result.deleted_files == ()


def test_explain_of_a_read_is_qualified_and_explain_analyze_is_refused(
    adapter: SparkIcebergAdapter, spark: FakeSpark
) -> None:
    adapter.query("EXPLAIN EXTENDED SELECT count(*) FROM orders", ["orders"])
    [text] = [s for s in spark.statements if s.startswith("EXPLAIN")]
    assert text.startswith("EXPLAIN EXTENDED SELECT")
    assert "`db`.`orders`" in text or "db.orders" in text
    with pytest.raises(UnsupportedAction):
        adapter.query("EXPLAIN ANALYZE SELECT * FROM orders", ["orders"])
    with pytest.raises(UnsupportedAction):
        adapter.query("EXPLAIN DELETE FROM orders", ["orders"])
