"""Shared fixtures: every test gets its own throwaway Iceberg warehouse under tmp_path."""

from __future__ import annotations

from datetime import datetime

import pyarrow as pa
import pytest

from sqlguard.core.catalog import LakehouseCatalog

LABELS_SCHEMA = {"user_id": "BIGINT", "label_ts": "TIMESTAMP", "churned": "BIGINT"}
USER_STATS_SCHEMA = {"user_id": "BIGINT", "updated_at": "TIMESTAMP", "total_spend": "DOUBLE"}


@pytest.fixture
def schema() -> dict[str, dict[str, str]]:
    return {"labels": dict(LABELS_SCHEMA), "user_stats": dict(USER_STATS_SCHEMA)}


@pytest.fixture
def catalog(tmp_path) -> LakehouseCatalog:
    return LakehouseCatalog.local(tmp_path / "warehouse")


@pytest.fixture
def orders_catalog(catalog: LakehouseCatalog) -> LakehouseCatalog:
    """Catalog with a small `orders` table (3 rows, one snapshot)."""
    catalog.create_table(
        "orders",
        pa.table(
            {
                "id": pa.array([1, 2, 3], pa.int64()),
                "customer": pa.array(["alice", "bob", "testa"]),
                "amount": pa.array([10.0, 20.0, 30.0]),
                "created_at": pa.array(
                    [datetime(2026, 1, d) for d in (1, 2, 3)], pa.timestamp("us")
                ),
            }
        ),
    )
    return catalog
