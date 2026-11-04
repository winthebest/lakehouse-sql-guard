"""Integration tests on the Docker stack of `docker/compose.lakehouse.yml`.

They need real catalogs, an S3-compatible store, Trino and Spark, so they are deselected unless
`SQLGUARD_INTEGRATION=1`. `docker/run-integration.sh` starts the stack, sets that variable, runs
the tests inside the compose network (the `runner` service) and tears everything down.

Connection details come from `SQLGUARD_IT_*` variables (set by the compose file); a catalog
whose variables are missing is skipped. Demo credentials only.
"""

from __future__ import annotations

import logging
import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pyarrow as pa
import pytest
from pyiceberg.catalog import load_catalog

from sqlguard.core.catalog import LakehouseCatalog

HERE = Path(__file__).parent
ROWS = 20

_log = logging.getLogger(__name__)


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "integration: needs the Docker stack of docker/compose.lakehouse.yml"
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    ours = [item for item in items if HERE in Path(str(item.fspath)).parents]
    for item in ours:
        item.add_marker(pytest.mark.integration)
    if os.environ.get("SQLGUARD_INTEGRATION") == "1":
        return
    config.hook.pytest_deselected(items=ours)
    items[:] = [item for item in items if item not in ours]


def setting(name: str, prefix: str = "SQLGUARD_IT_") -> str:
    value = os.environ.get(f"{prefix}{name}")
    if not value:
        pytest.skip(f"{prefix}{name} is not set")
    return value


@dataclass(frozen=True)
class CatalogUnderTest:
    name: str
    properties: dict[str, str]


def _static_s3(region: str) -> dict[str, str]:
    return {
        "s3.endpoint": setting("REST_S3_ENDPOINT"),
        "s3.access-key-id": setting("S3_KEY"),
        "s3.secret-access-key": setting("S3_SECRET"),
        "s3.path-style-access": "true",
        "s3.region": region,
        # Neither catalog vends credentials; do not ask for them.
        "header.X-Iceberg-Access-Delegation": "",
    }


def catalog_properties(name: str) -> dict[str, str]:
    if name == "lakekeeper":
        # Lakekeeper vends short-lived STS credentials (PyIceberg asks by default).
        return {
            "type": "rest",
            "uri": setting("LAKEKEEPER_URI"),
            "warehouse": setting("LAKEKEEPER_WAREHOUSE"),
        }
    if name == "rest":
        return {"type": "rest", "uri": setting("REST_URI"), **_static_s3("us-east-1")}
    if name == "polaris":
        return {
            "type": "rest",
            "uri": setting("POLARIS_URI"),
            "warehouse": setting("POLARIS_WAREHOUSE"),
            "credential": setting("POLARIS_CREDENTIAL"),
            "scope": "PRINCIPAL_ROLE:ALL",
            "oauth2-server-uri": f"{setting('POLARIS_URI')}/v1/oauth/tokens",
            "header.Polaris-Realm": "POLARIS",
            **_static_s3("us-west-2"),
        }
    raise ValueError(name)


@pytest.fixture(params=["lakekeeper", "rest", "polaris"])
def catalog_under_test(request: pytest.FixtureRequest) -> CatalogUnderTest:
    return CatalogUnderTest(request.param, catalog_properties(request.param))


def _orders() -> pa.Table:
    return pa.table(
        {
            "id": pa.array(range(1, ROWS + 1), pa.int64()),
            "customer": pa.array([f"c{i % 4}" for i in range(ROWS)]),
            "amount": pa.array([float(i) for i in range(ROWS)]),
        }
    )


@pytest.fixture
def lake(catalog_under_test: CatalogUnderTest) -> Iterator[LakehouseCatalog]:
    """A fresh namespace in the catalog under test with `orders` (ids 1..20, one snapshot).

    Opened through `LakehouseCatalog.from_config`, the way a user's configuration would be.
    """
    yield from _lake(catalog_under_test.name, catalog_under_test.properties)


def _lake(name: str, properties: dict[str, str]) -> Iterator[LakehouseCatalog]:
    namespace = f"it_{uuid.uuid4().hex[:10]}"  # lower case: Trino folds identifiers
    raw = load_catalog(name, **properties)
    raw.create_namespace(namespace)
    lake = LakehouseCatalog.from_config(name, properties, namespace)
    lake.create_table("orders", _orders())
    try:
        yield lake
    finally:
        try:
            for identifier in raw.list_tables(namespace):
                raw.purge_table(identifier)
            raw.drop_namespace(namespace)
        except Exception:  # noqa: BLE001 - the stack is torn down after the run; do not mask a test failure
            _log.warning("could not clean up namespace %s", namespace, exc_info=True)


@pytest.fixture
def lakekeeper_lake() -> Iterator[LakehouseCatalog]:
    """Like `lake`, on Lakekeeper only (the catalog Trino and Spark are wired to)."""
    yield from _lake("lakekeeper", catalog_properties("lakekeeper"))


def order_ids(lake: LakehouseCatalog, table: str = "orders") -> list[int]:
    return sorted(lake.read(table).column("id").to_pylist())


def main_id(lake: LakehouseCatalog, table: str = "orders") -> int:
    return lake.table(table).current_snapshot().snapshot_id
