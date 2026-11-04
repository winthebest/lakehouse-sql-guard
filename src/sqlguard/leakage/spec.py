"""LeakageSpec: what the user declares about time and labels, loaded from YAML.

Nothing is inferred from column names. Every table the query reads must be classified as
the label table, a temporal table (with its event-time column), a static table, or an
outcome table; otherwise the validator reports it as unclassified.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from sqlguard.core.lineage import SourceColumn


class SpecError(ValueError):
    """Raised when a spec file is missing a required field or is inconsistent."""


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

    def __post_init__(self) -> None:
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
    def target_sources(self) -> set[SourceColumn]:
        """Source columns that encode the outcome; no feature may depend on them."""
        labels = {SourceColumn(self.label_table, c) for c in self.label_columns}
        return labels | set(self.outcome_columns)

    def classify(self, table: str) -> str:
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
            outcome.append(SourceColumn(table, column))
        return cls(
            label_table=data["label_table"],
            entity_keys=tuple(data["entity_keys"]),
            prediction_ts=data["prediction_ts"],
            label_columns=tuple(data["label_columns"]),
            time_columns=dict(data["time_columns"]),
            availability_columns=dict(data.get("availability_columns") or {}),
            static_tables=tuple(data.get("static_tables") or ()),
            outcome_columns=tuple(outcome),
            dialect=data.get("dialect", "duckdb"),
        )

    @classmethod
    def from_yaml(cls, path: Path | str) -> LeakageSpec:
        with open(path, encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
        if not isinstance(data, dict):
            raise SpecError(f"{path}: expected a mapping at the top level")
        return cls.from_dict(data)
