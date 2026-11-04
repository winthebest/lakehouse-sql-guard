"""End-to-end demo of the Guard on a real Spark + Iceberg REST + S3 stack.

Run it with `docker/demo.sh`: that starts the stack of docker/compose.lakehouse.yml (Lakekeeper
on an S3-compatible store, plus Spark), runs this file in the `runner` container (the catalog
vends storage credentials for a host name only the compose network resolves) and tears the
stack down again.

The agent side uses the same tool layer the MCP server exposes (`LakehouseTools`), so what the
agent sees here is what an MCP client sees. The reviewer side calls `Guard.submit` with an
approver, which is the out-of-band step an operator performs.

  1. The agent's DELETE forgot half its filter and would remove 30% of the table: held.
  2. The agent corrects it to the 3 rows it meant: rehearsed on a branch, published.
  3. A bulk archive touching 25% of the table is held; a reviewer approves it: published.
  4. Expiring old snapshots is planned (what would go) and needs approval: held.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from pyiceberg.catalog.rest import RestCatalog
from pyspark.sql import SparkSession

from sqlguard.core.catalog import LakehouseCatalog
from sqlguard.mcp_server import LakehouseTools
from sqlguard.safety import ApprovalRequest, Guard, Policy, SqlAction, TaskScope
from sqlguard.safety.executor import SparkIcebergAdapter
from sqlguard.safety.tools import ToolResult

REMOTE = os.environ.get("SQLGUARD_SPARK_REMOTE", "sc://spark:15002")
REST_URI = os.environ.get("SQLGUARD_REST_URI", "http://lakekeeper:8181/catalog")
REST_WAREHOUSE = os.environ.get("SQLGUARD_REST_WAREHOUSE", "demo")
CATALOG = os.environ.get("SQLGUARD_SPARK_CATALOG", "demo")
NAMESPACE = "db"
POLICY = Path(__file__).with_name("demo-policy.yaml")

WRONG_DELETE = "DELETE FROM orders WHERE status = 'cancelled'"
FIXED_DELETE = "DELETE FROM orders WHERE status = 'cancelled' AND customer_id = 1 AND id <= 30"
BULK_ARCHIVE = "UPDATE orders SET status = 'archived' WHERE id <= 25"


class EchoSession:
    """Prints the Spark statements that change table state, so the procedure calls are visible."""

    def __init__(self, session: SparkSession) -> None:
        self._session = session

    def sql(self, text: str):
        if text.startswith(("CALL", "ALTER TABLE")):
            print(f"      spark> {text}")
        return self._session.sql(text)


def heading(text: str) -> None:
    print(f"\n=== {text}")


def show(result: ToolResult) -> dict:
    payload = result.payload
    print(f"    status: {result.status}   outcome: {payload.get('outcome')}")
    for reason in payload.get("reasons", [])[:3]:
        print(f"    reason: {reason}")
    for impact in payload.get("impact", []):
        print(
            f"    impact: {impact['rows_deleted']} deleted, {impact['rows_modified']} modified, "
            f"{impact['rows_added']} added of {impact['rows_before']} rows"
        )
    if plan := payload.get("plan"):
        print(
            f"    plan:   {plan['kind']} would expire {plan['expired_snapshot_ids']} "
            f"and keep {plan['remaining_snapshots']}"
        )
    return payload


def seed(spark: SparkSession) -> None:
    spark.sql(f"CREATE NAMESPACE IF NOT EXISTS {CATALOG}.{NAMESPACE}").collect()
    table = f"{CATALOG}.{NAMESPACE}.orders"
    spark.sql(f"DROP TABLE IF EXISTS {table} PURGE").collect()
    spark.sql(
        f"CREATE TABLE {table} (id BIGINT, customer_id INT, status STRING, amount DOUBLE) "
        "USING iceberg"
    ).collect()
    # Four inserts make four snapshots. Orders with id % 10 < 3 are cancelled: 30 of 100.
    for start in (1, 26, 51, 76):
        spark.sql(
            f"INSERT INTO {table} SELECT id, CAST(id % 10 AS INT) + 1, "
            "CASE WHEN id % 10 < 3 THEN 'cancelled' ELSE 'paid' END, CAST(id AS DOUBLE) * 1.5 "
            f"FROM range({start}, {start + 25})"
        ).collect()


def count(lake: LakehouseCatalog) -> int:
    return lake.read("orders").num_rows


def snapshot(lake: LakehouseCatalog) -> int | None:
    current = lake.table("orders").current_snapshot()
    return current.snapshot_id if current else None


def main() -> int:
    try:
        spark = SparkSession.builder.remote(REMOTE).getOrCreate()
        spark.sql("SELECT 1").collect()
    except Exception as err:  # noqa: BLE001 - any failure here means the stack is not up
        print(f"cannot reach Spark Connect at {REMOTE}: {err}", file=sys.stderr)
        print("run it with docker/demo.sh, which starts the stack for you", file=sys.stderr)
        return 2

    rest = RestCatalog(CATALOG, uri=REST_URI, warehouse=REST_WAREHOUSE)  # vended credentials
    lake = LakehouseCatalog(rest, NAMESPACE)
    seed(spark)

    audit_path = Path(tempfile.mkdtemp(prefix="sqlguard-demo-")) / "audit.jsonl"
    policy = Policy.load(POLICY)
    adapter = SparkIcebergAdapter(lake, EchoSession(spark), CATALOG)
    guard = Guard(lake, policy, audit_path=audit_path, adapter=adapter)
    scope = TaskScope.of(namespaces=[NAMESPACE])
    agent = LakehouseTools.create(guard, scope, Path.cwd(), Path.cwd())

    heading(f"Seeded db.orders: {count(lake)} rows, snapshot {snapshot(lake)}")

    heading("1. Agent runs a DELETE that lost half of its filter")
    print(f"    sql: {WRONG_DELETE}")
    before = snapshot(lake)
    result = agent.call("run_sql", {"sql": WRONG_DELETE})
    show(result)
    assert result.status == "needs_approval", result.status
    assert snapshot(lake) == before and count(lake) == 100
    print(f"    -> held. db.orders still has {count(lake)} rows on snapshot {snapshot(lake)}.")

    heading("2. Agent corrects the DELETE to the 3 rows it meant")
    print(f"    sql: {FIXED_DELETE}")
    result = agent.call("run_sql", {"sql": FIXED_DELETE})
    show(result)
    assert result.status == "ok" and result.guard and result.guard["published"], result
    print(f"    -> published by snapshot id: {count(lake)} rows, snapshot {snapshot(lake)}.")

    heading("3. Agent archives 25% of the table; a reviewer approves")
    print(f"    sql: {BULK_ARCHIVE}")
    result = agent.call("run_sql", {"sql": BULK_ARCHIVE})
    show(result)
    assert result.status == "needs_approval", result.status
    before = snapshot(lake)

    def reviewer(request: ApprovalRequest) -> bool:
        measured = request.impact.tables[0] if request.impact else None
        print(f"    reviewer sees: {measured.rows_modified if measured else '?'} rows modified")
        print("    reviewer approves")
        return True

    reviewer.approver_id = "demo-reviewer"  # type: ignore[attr-defined]  # recorded in the audit

    approved = guard.submit(SqlAction(BULK_ARCHIVE), scope, approver=reviewer)
    print(f"    outcome: {approved.outcome.value}   published: {approved.published}")
    assert approved.published and snapshot(lake) != before
    print(f"    -> published by snapshot id: snapshot {before} -> {snapshot(lake)}.")
    left = [r for r in rest.load_table(f"{NAMESPACE}.orders").refs() if r != "main"]
    assert not left, f"audit branches left behind: {left}"

    heading("4. Agent expires old snapshots: planned, needs approval")
    cutoff = datetime.now(UTC).isoformat()
    kept = len(lake.snapshots("orders"))
    result = agent.call(
        "expire_snapshots", {"table": "orders", "older_than": cutoff, "retain_last": 2}
    )
    show(result)
    assert result.status == "needs_approval", result.status
    assert len(lake.snapshots("orders")) == kept
    print(f"    -> held. All {kept} snapshots are still there.")

    heading("Audit trail (held actions have no approver; the approved one names its reviewer)")
    for line in audit_path.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        if record["phase"] != "completed":
            continue
        action = record["action"]
        text = action.get("sql") or action.get("kind") or action
        who = record["approver"] or "-"
        print(
            f"    {record['outcome']:<10} approval={record['approval']:<12} by={who:<26} "
            f"published={record['published']!s:<5} {text}"
        )
    print(f"\n    audit log: {audit_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
