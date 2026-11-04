"""LeakageSpec: what the user declares about time and labels, loaded from YAML.

Nothing is inferred from column names. Every table the query reads must be classified as
the label table, a temporal table (with its event-time column), a static table, or an
outcome table; otherwise the validator reports it as unclassified. Names are compared
case-insensitively (stored lowercase), matching how unquoted SQL identifiers resolve.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from sqlguard.core.lineage import SourceColumn


class SpecError(ValueError):
    """Raised when a spec file is missing a required field or is inconsistent."""


def _name(value: Any, key: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SpecError(f"{key} must be a non-empty string, got {value!r}")
    return value.strip().lower()


def _names(value: Any, key: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise SpecError(f"{key} must be a non-empty list, got {value!r}")
    return tuple(_name(v, key) for v in value)


_DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(second|minute|hour|day|week)s?\s*$", re.IGNORECASE)
_DURATION_SECONDS = {"second": 1, "minute": 60, "hour": 3600, "day": 86_400, "week": 604_800}
BOUNDARIES = ("strict", "inclusive")


def parse_duration(value: Any, key: str) -> float:
    """Seconds in a duration such as `2 days`, `36 hours` or `1 week` (fixed-length units only)."""
    match = _DURATION.match(value) if isinstance(value, str) else None
    if match is None or float(match.group(1)) <= 0:
        raise SpecError(
            f"{key} must be a positive duration like '2 days' (seconds, minutes, hours, days or "
            f"weeks), got {value!r}"
        )
    return float(match.group(1)) * _DURATION_SECONDS[match.group(2).lower()]


def relation_key(name: str) -> str:
    """Canonical form of a possibly qualified relation name: unquoted, lowercase, dotted."""
    parts = [p.strip().strip('"`').lower() for p in str(name).split(".")]
    if not 1 <= len(parts) <= 3 or not all(parts):
        raise SpecError(f"relation names have one to three parts (catalog.schema.table): {name!r}")
    return ".".join(parts)


def _durations(value: Any, key: str, tables: dict[str, str]) -> dict[str, float]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise SpecError(f"{key} must be a mapping of table -> duration, got {value!r}")
    found = {_name(t, key): parse_duration(d, f"{key}[{t}]") for t, d in value.items()}
    unknown = set(found) - set(tables)
    if unknown:
        raise SpecError(f"{key} names tables without a time column: {sorted(unknown)}")
    return found


def _relations(value: Any) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise SpecError(f"relations must be a mapping of relation name -> table, got {value!r}")
    return {relation_key(r): relation_key(t) for r, t in value.items()}


def _mapping(value: Any, key: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise SpecError(f"{key} must be a mapping of table -> column, got {value!r}")
    return {_name(t, key): _name(c, key) for t, c in value.items()}


@dataclass(frozen=True)
class LeakageSpec:
    label_table: str
    entity_keys: tuple[str, ...]
    prediction_ts: str
    label_columns: tuple[str, ...]
    # table -> column holding the time the fact happened (event / validity time)
    time_columns: dict[str, str]
    # table -> column holding the time the fact became available (ingestion time)
    availability_columns: dict[str, str] = field(default_factory=dict)
    static_tables: tuple[str, ...] = ()
    outcome_columns: tuple[SourceColumn, ...] = ()
    dialect: str = "duckdb"
    # `strict`: facts stamped exactly at prediction_ts are not yet known; `inclusive`: they are
    boundary: str = "strict"
    # table -> seconds: the longest a fact takes to become available after its event time. An
    # event-time bound at least this far before prediction_ts is as good as an availability bound.
    max_ingest_delay: dict[str, float] = field(default_factory=dict)
    # seconds after a label row's prediction_ts at which its label is final; labels older than
    # that (inclusive) may be aggregated into features
    label_known_after: float | None = None
    # SQL relation name (`table`, `schema.table`, `catalog.schema.table`) -> catalog table
    relations: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.boundary not in BOUNDARIES:
            raise SpecError(f"boundary must be one of {BOUNDARIES}, got {self.boundary!r}")
        unknown_delay = set(self.max_ingest_delay) - set(self.time_columns)
        if unknown_delay:
            raise SpecError(f"max_ingest_delay names tables without a time column: {unknown_delay}")
        unknown = set(self.availability_columns) - set(self.time_columns)
        if unknown:
            raise SpecError(f"availability_columns for tables without a time column: {unknown}")
        overlap = set(self.static_tables) & set(self.time_columns)
        if overlap:
            raise SpecError(f"tables declared both static and temporal: {overlap}")
        if self.label_table in self.time_columns or self.label_table in self.static_tables:
            raise SpecError("label_table must not also be declared temporal or static")

    @property
    def outcome_tables(self) -> set[str]:
        return {c.table for c in self.outcome_columns}

    @property
    def passthrough_columns(self) -> set[str]:
        """Output columns that identify the row or carry the target, not features."""
        return {*self.entity_keys, self.prediction_ts, *self.label_columns}

    @property
    def key_columns(self) -> list[str]:
        """Columns that identify one training row (used to align rows in replay)."""
        return [*self.entity_keys, self.prediction_ts]

    @property
    def target_sources(self) -> set[SourceColumn]:
        """Source columns that encode the outcome; no feature may depend on them."""
        labels = {SourceColumn(self.label_table, c) for c in self.label_columns}
        return labels | set(self.outcome_columns)

    def classify(self, table: str) -> str:
        table = table.lower()
        if table == self.label_table:
            return "label"
        if table in self.time_columns:
            return "temporal"
        if table in self.static_tables:
            return "static"
        if table in self.outcome_tables:
            return "outcome"
        return "unclassified"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LeakageSpec:
        required = ["label_table", "entity_keys", "prediction_ts", "label_columns", "time_columns"]
        missing = [key for key in required if not data.get(key)]
        if missing:
            raise SpecError(f"LeakageSpec is missing required fields: {', '.join(missing)}")
        outcome = []
        for ref in data.get("outcome_columns") or []:
            table, _, column = str(ref).partition(".")
            if not column:
                raise SpecError(f"outcome_columns entries must be 'table.column', got {ref!r}")
            outcome.append(SourceColumn(table.lower(), column.lower()))
        static = data.get("static_tables") or []
        times = _mapping(data["time_columns"], "time_columns")
        return cls(
            label_table=_name(data["label_table"], "label_table"),
            entity_keys=_names(data["entity_keys"], "entity_keys"),
            prediction_ts=_name(data["prediction_ts"], "prediction_ts"),
            label_columns=_names(data["label_columns"], "label_columns"),
            time_columns=times,
            availability_columns=_mapping(data.get("availability_columns"), "availability"),
            static_tables=_names(static, "static_tables") if static else (),
            outcome_columns=tuple(outcome),
            dialect=data.get("dialect", "duckdb"),
            boundary=str(data.get("boundary", "strict")).strip().lower(),
            max_ingest_delay=_durations(data.get("max_ingest_delay"), "max_ingest_delay", times),
            label_known_after=(
                None
                if data.get("label_known_after") is None
                else parse_duration(data["label_known_after"], "label_known_after")
            ),
            relations=_relations(data.get("relations")),
        )

    @classmethod
    def from_yaml(cls, path: Path | str) -> LeakageSpec:
        with open(path, encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
        if not isinstance(data, dict):
            raise SpecError(f"{path}: expected a mapping at the top level")
        return cls.from_dict(data)
