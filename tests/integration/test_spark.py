"""SparkIcebergAdapter on Lakekeeper: the publish path and Spark's table cache, for real.

The bulk of the Spark adapter checks (branch write, stale publish, rollback, expiry, orphan dry
run) are the `spark`-marked tests of `tests/safety/test_spark_adapter.py`, which the integration
run executes against Lakekeeper too. This module covers what needs the whole stack: Spark seeing
a main that PyIceberg moved, and Spark writing under a Trino reader.
"""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pytest

from sqlguard.core.catalog import LakehouseCatalog
from sqlguard.safety import Policy, SqlAction, TaskScope
from sqlguard.safety.executor import BranchDiverged, SparkIcebergAdapter
from sqlguard.safety.executor.composite import CompositeAdapter
from sqlguard.safety.executor.trino_iceberg import TrinoIcebergAdapter
from sqlguard.safety.guard import Guard, Outcome
from tests.integration.conftest import ROWS, main_id, order_ids, setting


@pytest.fixture(scope="module")
def spark_session():
    pyspark_sql = pytest.importorskip("pyspark.sql", reason="install the `spark` extra")
    remote = setting("SPARK_REMOTE", "SQLGUARD_")
    try:
        session = pyspark_sql.SparkSession.builder.remote(remote).getOrCreate()
        session.sql("SELECT 1").collect()
    except Exception as err:  # noqa: BLE001 - any connection failure means the stack is down
        pytest.fail(f"no Spark Connect server at {remote}: {err}")
    yield session
    session.stop()


@pytest.fixture
def lake(lakekeeper_lake: LakehouseCatalog) -> LakehouseCatalog:
    return lakekeeper_lake


@pytest.fixture
def spark(lake: LakehouseCatalog, spark_session) -> SparkIcebergAdapter:
    spark_session.sql("SET spark.sql.session.timeZone = UTC")
    return SparkIcebergAdapter(lake, spark_session, setting("SPARK_CATALOG", "SQLGUARD_"))


def spark_count(spark: SparkIcebergAdapter, lake: LakehouseCatalog) -> int:
    ident = f"`{spark.spark_catalog}`.`{lake.namespace}`.`orders`"
    return spark.spark.sql(f"SELECT count(*) FROM {ident}").collect()[0][0]


def publish(spark: SparkIcebergAdapter, branch):
    recorded = spark.diff(branch).branch
    return spark.publish(recorded, recorded.head)


def test_spark_sees_main_after_a_pyiceberg_publish(
    spark: SparkIcebergAdapter, lake: LakehouseCatalog
):
    """Publishing is a PyIceberg commit; the adapter must refresh Spark's cached table."""
    assert spark_count(spark, lake) == ROWS  # Spark has now loaded (and cached) the table
    branch = spark.create_branch("orders")
    spark.apply_sql(branch, "DELETE FROM orders WHERE id > 15")
    assert spark_count(spark, lake) == ROWS  # branch writes do not touch main

    result = publish(spark, branch)
    assert result.changed and main_id(lake) == result.snapshot_id
    assert spark_count(spark, lake) == 15  # without REFRESH TABLE Spark could still say 20
    assert order_ids(lake) == list(range(1, 16))
    spark.drop_branch(branch)


def test_spark_publish_after_main_moved_is_refused(
    spark: SparkIcebergAdapter, lake: LakehouseCatalog
):
    branch = spark.create_branch("orders")
    spark.apply_sql(branch, "DELETE FROM orders WHERE id > 15")
    lake.table("orders").append(
        pa.table({"id": pa.array([500], pa.int64()), "customer": ["z"], "amount": pa.array([1.0])})
    )
    with pytest.raises(BranchDiverged):
        publish(spark, branch)
    assert 500 in order_ids(lake)
    assert spark_count(spark, lake) == ROWS + 1


def test_trino_reads_and_spark_writes_through_the_guard(
    spark: SparkIcebergAdapter, lake: LakehouseCatalog, tmp_path: Path
):
    trino = TrinoIcebergAdapter.connect(
        lake,
        host=setting("TRINO_HOST"),
        port=int(setting("TRINO_PORT")),
        trino_catalog=setting("TRINO_CATALOG"),
    )
    composite = CompositeAdapter(read=trino, write=spark)
    guard = Guard(lake, Policy(), audit_path=tmp_path / "audit.jsonl", adapter=composite)
    scope = TaskScope.of(namespaces=[lake.namespace])

    result = guard.submit(SqlAction("DELETE FROM orders WHERE id = 20"), scope, lambda r: False)
    assert result.outcome is Outcome.PUBLISHED and result.published
    assert order_ids(lake) == list(range(1, ROWS))
    assert sorted(trino.read("orders").column("id").to_pylist()) == list(range(1, ROWS))
    assert spark_count(spark, lake) == ROWS - 1
    assert set(lake.refs("orders")) == {"main"}
    assert {"id", "customer", "amount"} == set(trino.read_ref("orders", "main").column_names)
