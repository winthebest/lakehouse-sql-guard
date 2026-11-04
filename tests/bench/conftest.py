import pytest

from bench.leakage.world import WorldConfig, build_world
from sqlguard.core.catalog import LakehouseCatalog


@pytest.fixture(scope="session")
def world(tmp_path_factory):
    """One synthetic world per test session (building it takes about a second)."""
    catalog = LakehouseCatalog.local(tmp_path_factory.mktemp("world"))
    summary = build_world(catalog, WorldConfig(seed=42))
    return catalog, summary
