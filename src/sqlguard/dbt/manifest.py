"""Read a dbt `manifest.json` (schema v12) without importing dbt.

Only the fields the leakage check needs are kept. `compiled_code` is present only for nodes that
`dbt compile` (or `build`/`run`) processed; `dbt parse` leaves it out.

Meta is read from `config.meta` and falls back to the node's `meta`, which dbt mirrors there;
the leakage settings live under the `leakage` key.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlglot.dialects.dialect import Dialect

SUPPORTED_SCHEMA = "/v12.json"  # dbt-core 1.8 to 1.12
KINDS = ("model", "seed", "snapshot", "source")
META_KEY = "leakage"
ROLES = ("training_set", "label_spine", "static")
# dbt adapter types whose SQL sqlglot reads under a different dialect name
_DIALECTS = {"sqlserver": "tsql", "fabric": "tsql"}


class ManifestError(ValueError):
    """The manifest cannot be used: wrong schema version, damaged, or missing what a check needs."""


@dataclass(frozen=True)
class Column:
    name: str
    data_type: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Node:
    """A model, seed, snapshot or source of the manifest."""

    unique_id: str
    name: str
    kind: str  # model | seed | snapshot | source
    relation_name: str | None
    original_file_path: str
    compiled_code: str | None = None
    depends_on: tuple[str, ...] = ()
    meta: dict[str, Any] = field(default_factory=dict)
    columns: dict[str, Column] = field(default_factory=dict)
    materialized: str | None = None

    @property
    def leakage(self) -> dict[str, Any]:
        """The node's `meta.leakage` settings (empty when it declares none)."""
        found = self.meta.get(META_KEY)
        if found is None:
            return {}
        if not isinstance(found, dict):
            raise ManifestError(
                f"{self.unique_id}: meta.{META_KEY} must be a mapping, got {found!r} "
                f"({self.original_file_path})"
            )
        return found

    @property
    def role(self) -> str | None:
        """The declared role; an unknown one is an error, never a model that goes unchecked."""
        role = self.leakage.get("role")
        if role is not None and role not in ROLES:
            raise ManifestError(
                f"{self.unique_id}: meta.{META_KEY}.role {role!r} is not one of {', '.join(ROLES)} "
                f"({self.original_file_path})"
            )
        return role


@dataclass(frozen=True)
class Manifest:
    path: str
    adapter: str
    dialect: str
    nodes: dict[str, Node]

    def get(self, unique_id: str) -> Node:
        try:
            return self.nodes[unique_id]
        except KeyError:
            raise ManifestError(f"{unique_id} is not in the manifest") from None

    def with_role(self, role: str) -> list[Node]:
        return [n for n in self.nodes.values() if n.kind == "model" and n.role == role]


def _mapping(value: Any, where: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ManifestError(f"{where}: expected a mapping, got {type(value).__name__}")
    return value


def _meta(raw: dict[str, Any], where: str) -> dict[str, Any]:
    """`config.meta`, else `meta`."""
    configured = _mapping(_mapping(raw.get("config"), f"{where}.config").get("meta"), where)
    return configured or _mapping(raw.get("meta"), f"{where}.meta")


def _columns(raw: dict[str, Any], where: str) -> dict[str, Column]:
    columns = {}
    for name, column in _mapping(raw.get("columns"), f"{where}.columns").items():
        column = _mapping(column, f"{where}.columns.{name}")
        columns[str(name)] = Column(
            str(column.get("name") or name),
            column.get("data_type") or None,
            _meta(column, f"{where}.columns.{name}"),
        )
    return columns


def _node(unique_id: str, raw: Any, kind: str) -> Node:
    raw = _mapping(raw, unique_id)
    config = _mapping(raw.get("config"), f"{unique_id}.config")
    depends = _mapping(raw.get("depends_on"), f"{unique_id}.depends_on").get("nodes") or []
    if not isinstance(depends, list):
        raise ManifestError(f"{unique_id}.depends_on.nodes must be a list")
    return Node(
        unique_id=unique_id,
        name=str(raw.get("name") or unique_id.rsplit(".", 1)[-1]),
        kind=kind,
        relation_name=raw.get("relation_name"),
        original_file_path=str(raw.get("original_file_path") or ""),
        compiled_code=raw.get("compiled_code") or None,
        depends_on=tuple(str(d) for d in depends),
        meta=_meta(raw, unique_id),
        columns=_columns(raw, unique_id),
        materialized=config.get("materialized"),
    )


def sqlglot_dialect(adapter: str) -> str:
    """The sqlglot dialect that reads the SQL of a dbt adapter."""
    name = _DIALECTS.get(adapter, adapter)
    if Dialect.get(name) is None:
        raise ManifestError(
            f"adapter {adapter!r} has no matching SQL dialect; set `dialect` in the --spec file"
        )
    return name


def parse_manifest(data: Any, path: str = "<manifest>") -> Manifest:
    if not isinstance(data, dict):
        raise ManifestError(f"{path}: expected a JSON object at the top level")
    metadata = _mapping(data.get("metadata"), f"{path}.metadata")
    version = str(metadata.get("dbt_schema_version") or "")
    if not version.endswith(SUPPORTED_SCHEMA):
        raise ManifestError(
            f"{path}: unsupported manifest schema {version or '(none)'!r}; only manifest "
            f"v12 is read (dbt-core 1.8 to 1.12). Regenerate it with a supported dbt-core."
        )
    adapter = str(metadata.get("adapter_type") or "")
    if not adapter:
        raise ManifestError(f"{path}: metadata.adapter_type is missing")
    nodes: dict[str, Node] = {}
    for unique_id, raw in _mapping(data.get("nodes"), f"{path}.nodes").items():
        kind = _mapping(raw, unique_id).get("resource_type")
        if kind in KINDS:
            nodes[unique_id] = _node(unique_id, raw, kind)
    for unique_id, raw in _mapping(data.get("sources"), f"{path}.sources").items():
        nodes[unique_id] = _node(unique_id, raw, "source")
    return Manifest(path, adapter, sqlglot_dialect(adapter), nodes)


def load_manifest(path: Path | str) -> Manifest:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except json.JSONDecodeError as err:
        raise ManifestError(f"{path}: not valid JSON ({err})") from None
    return parse_manifest(data, str(path))


def load_column_types(path: Path | str) -> dict[str, dict[str, str]]:
    """`catalog.json` (from `dbt docs generate`): unique_id -> {column: type}, lowercase names."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except json.JSONDecodeError as err:
        raise ManifestError(f"{path}: not valid JSON ({err})") from None
    found: dict[str, dict[str, str]] = {}
    for section in ("nodes", "sources"):
        for unique_id, entry in _mapping(_mapping(data, str(path)).get(section), section).items():
            columns = _mapping(_mapping(entry, unique_id).get("columns"), unique_id)
            found[unique_id] = {
                str(name).lower(): str(_mapping(col, name).get("type") or "")
                for name, col in columns.items()
            }
    return found
