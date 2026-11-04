"""Build small dbt manifests (schema v12) for tests, without running dbt."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

FIXTURE = Path(__file__).parent / "fixtures" / "manifest_v12.json"
V12 = "https://schemas.getdbt.com/dbt/manifest/v12.json"


def load_fixture() -> dict[str, Any]:
    return copy.deepcopy(json.loads(FIXTURE.read_text(encoding="utf-8")))


def relation(schema: str, name: str, database: str = "db") -> str:
    return f'"{database}"."{schema}"."{name}"'


def columns(**types: str | None) -> dict[str, Any]:
    return {n: {"name": n, "data_type": t, "meta": {}} for n, t in types.items()}


def source(
    name: str,
    leakage: dict[str, Any] | None = None,
    cols: dict[str, Any] | None = None,
    schema: str = "raw",
    **meta: Any,
) -> tuple[str, dict[str, Any]]:
    unique_id = f"source.proj.{schema}.{name}"
    meta = {**meta, **({"leakage": leakage} if leakage else {})}
    return unique_id, {
        "unique_id": unique_id,
        "resource_type": "source",
        "name": name,
        "relation_name": relation(schema, name),
        "original_file_path": f"models/{schema}.yml",
        "config": {"meta": meta},
        "meta": meta,
        "columns": cols or {},
    }


def model(
    name: str,
    sql: str | None,
    deps: list[str] = (),
    leakage: dict[str, Any] | None = None,
    cols: dict[str, Any] | None = None,
    materialized: str = "view",
    schema: str = "main",
) -> tuple[str, dict[str, Any]]:
    unique_id = f"model.proj.{name}"
    meta = {"leakage": leakage} if leakage else {}
    return unique_id, {
        "unique_id": unique_id,
        "resource_type": "model",
        "name": name,
        "relation_name": None if materialized == "ephemeral" else relation(schema, name),
        "original_file_path": f"models/{name}.sql",
        "compiled_code": sql,
        "depends_on": {"nodes": list(deps)},
        "config": {"materialized": materialized, "meta": meta},
        "meta": meta,
        "columns": cols or {},
    }


def manifest(
    *entries: tuple[str, dict[str, Any]], version: str = V12, adapter: str = "duckdb"
) -> dict[str, Any]:
    """A manifest dict; sources and models are told apart by their resource type."""
    data: dict[str, Any] = {
        "metadata": {"dbt_schema_version": version, "adapter_type": adapter},
        "nodes": {},
        "sources": {},
    }
    for unique_id, node in entries:
        data["sources" if node["resource_type"] == "source" else "nodes"][unique_id] = node
    return data


def write(path: Path, data: dict[str, Any]) -> Path:
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


# A small project whose pieces tests recombine: a label spine, a temporal source, a static source.
SPINE = {
    "role": "label_spine",
    "entity_keys": ["user_id"],
    "prediction_ts": "label_ts",
    "label_columns": ["churned"],
}
LABELS = columns(user_id="bigint", label_ts="timestamp", churned="boolean")
EVENTS = columns(
    event_id="bigint",
    user_id="bigint",
    event_ts="timestamp",
    ingested_at="timestamp",
    amount="double",
)
USERS = columns(user_id="bigint", country="varchar")
TRAINING = {"role": "training_set"}

LABELS_ID, LABELS_NODE = source("labels", SPINE, LABELS)
EVENTS_ID, EVENTS_NODE = source(
    "events", {"time_column": "event_ts", "availability_column": "ingested_at"}, EVENTS
)
USERS_ID, USERS_NODE = source("users", {"role": "static"}, USERS)
BASE = [(LABELS_ID, LABELS_NODE), (EVENTS_ID, EVENTS_NODE), (USERS_ID, USERS_NODE)]

LEAKY_FEATURES = 'select user_id, count(*) as n_events from "db"."raw"."events" group by user_id'
TRAINING_SQL = (
    'select l.user_id, l.label_ts, l.churned, f.n_events from "db"."raw"."labels" as l '
    'left join "db"."main"."int_features" as f on f.user_id = l.user_id'
)
