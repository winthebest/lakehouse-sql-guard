"""LeakageSpec: what the user declares about time and labels, loaded from YAML.

Nothing is inferred from column names. Every table the query reads must be classified as
the label table, a temporal table (with its event-time column), a static table, or an
outcome table; otherwise the validator reports it as unclassified. The label table may also be
listed in `time_columns` (with its own availability column, ingest delay and time unit) when it
doubles as event history, as an interaction log does; it may never be static. Names are compared
case-insensitively (stored lowercase), matching how unquoted SQL identifiers resolve.

Optional fields: `entity_keys: []` for a single series (the key must still be written out),
`time_units` for integer time columns, `label_known_after: 0` for labels final the moment the
row is stamped, `output_roles` to say which output columns of the query are labels,
run metadata, or (when `features` is given) the only features, `validity_end_columns` for the
column that closes a version of a row (an SCD2 `valid_to`), and `unique_keys` for columns that
name one row of a table.
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


def _names(value: Any, key: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list) or not (value or allow_empty):
        raise SpecError(
            f"{key} must be a {'list' if allow_empty else 'non-empty list'}, got {value!r}"
        )
    return tuple(_name(v, key) for v in value)


_DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(second|minute|hour|day|week)s?\s*$", re.IGNORECASE)
_DURATION_SECONDS = {"second": 1, "minute": 60, "hour": 3600, "day": 86_400, "week": 604_800}
_UNIT_FREE_ZERO = re.compile(r"^\s*0(?:\.0+)?\s*$")
BOUNDARIES = ("strict", "inclusive")
TIME_UNITS = ("seconds", "milliseconds", "microseconds")
OUTPUT_ROLES = ("label", "metadata", "features")


def parse_duration(value: Any, key: str, *, allow_zero: bool = False) -> float:
    """Seconds in a duration such as `2 days`, `36 hours` or `1 week` (fixed-length units only).

    With `allow_zero`, `0` (a number or a string) is a duration with no unit, and `0 <unit>` is
    accepted too; negative values never are.
    """
    if allow_zero and not isinstance(value, bool):
        if isinstance(value, int | float) and value == 0:
            return 0.0
        if isinstance(value, str) and _UNIT_FREE_ZERO.match(value):
            return 0.0
    match = _DURATION.match(value) if isinstance(value, str) else None
    seconds = None
    if match is not None:
        seconds = float(match.group(1)) * _DURATION_SECONDS[match.group(2).lower()]
    if seconds is None or (seconds == 0 and not allow_zero):
        kind, zero = ("non-negative", " or 0") if allow_zero else ("positive", "")
        raise SpecError(
            f"{key} must be a {kind} duration like '2 days'{zero} (seconds, minutes, hours, days "
            f"or weeks), got {value!r}"
        )
    return seconds


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
    found = {}
    for relation, table in value.items():
        key = relation_key(relation)
        if "." not in key:
            raise SpecError(
                f"relations keys are qualified names (schema.table or catalog.schema.table), "
                f"got the bare name {relation!r}: bare names always mean the catalog table itself"
            )
        found[key] = relation_key(table)
    return found


def _mapping(value: Any, key: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise SpecError(f"{key} must be a mapping of table -> column, got {value!r}")
    return {_name(t, key): _name(c, key) for t, c in value.items()}


def _time_units(value: Any) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise SpecError(f"time_units must be a mapping of table -> unit, got {value!r}")
    found = {}
    for table, unit in value.items():
        found[_name(table, "time_units")] = (
            _name(unit, "time_units") if isinstance(unit, str) else unit
        )
    return found


def _validity_end_columns(value: Any) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise SpecError(f"validity_end_columns must be a mapping of table -> column, got {value!r}")
    return {
        _name(t, "validity_end_columns"): _name(c, "validity_end_columns") for t, c in value.items()
    }


def _unique_keys(value: Any) -> dict[str, tuple[tuple[str, ...], ...]]:
    """`table -> [col, ...]` (one key) or `table -> [[col, ...], ...]` (several keys)."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise SpecError(f"unique_keys must be a mapping of table -> key columns, got {value!r}")
    found: dict[str, tuple[tuple[str, ...], ...]] = {}
    for table, keys in value.items():
        if not isinstance(keys, list) or not keys:
            raise SpecError(f"unique_keys[{table}] must be a non-empty list, got {keys!r}")
        listed = keys if all(isinstance(k, list) for k in keys) else [keys]
        found[_name(table, "unique_keys")] = tuple(
            _names(key, f"unique_keys[{table}]") for key in listed
        )
    return found


@dataclass(frozen=True)
class OutputRoles:
    """What the output columns of a query are, as far as the spec author documented them.

    `label` and `metadata` columns are not features. When `features` is given it is the complete
    list of feature columns: every other output column is not a feature.
    """

    label: tuple[str, ...] = ()
    metadata: tuple[str, ...] = ()
    features: tuple[str, ...] | None = None

    @classmethod
    def from_value(cls, value: Any) -> OutputRoles:
        if value is None:
            return cls()
        if not isinstance(value, dict):
            raise SpecError(f"output_roles must be a mapping of role -> columns, got {value!r}")
        unknown = set(value) - set(OUTPUT_ROLES)
        if unknown:
            raise SpecError(
                f"output_roles has unknown roles {sorted(map(str, unknown))}: use {OUTPUT_ROLES}"
            )
        roles = {
            role: _names(value[role], f"output_roles.{role}", allow_empty=True)
            for role in OUTPUT_ROLES
            if value.get(role) is not None
        }
        if "features" in roles and not roles["features"]:
            raise SpecError(
                "output_roles.features must not be empty: leave it out to declare no feature list"
            )
        return cls(**roles)


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
    # seconds after a label row's prediction_ts at which its label is final; labels at least that
    # old may be aggregated into features (judged like any bound: strictly before prediction_ts
    # unless `boundary` is inclusive)
    label_known_after: float | None = None
    # SQL relation name (`table`, `schema.table`, `catalog.schema.table`) -> catalog table
    relations: dict[str, str] = field(default_factory=dict)
    # table -> unit of its time column when that column is an integer (epoch seconds, ms, us)
    time_units: dict[str, str] = field(default_factory=dict)
    # which output columns of the query are labels, run metadata, or the only features
    output_roles: OutputRoles = field(default_factory=OutputRoles)
    # table -> the column that holds when a row's validity ended (an SCD2 `valid_to`): the time the
    # row was closed, in the clock of the table's time column, never a scheduled future date. A
    # bound on it limits what was known like a bound on the time column does
    validity_end_columns: dict[str, str] = field(default_factory=dict)
    # table -> sets of columns whose values never repeat in it: two reads of the label table joined
    # on one of them are the same row
    unique_keys: dict[str, tuple[tuple[str, ...], ...]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.boundary not in BOUNDARIES:
            raise SpecError(f"boundary must be one of {BOUNDARIES}, got {self.boundary!r}")
        unknown_delay = set(self.max_ingest_delay) - set(self.time_columns)
        if unknown_delay:
            raise SpecError(f"max_ingest_delay names tables without a time column: {unknown_delay}")
        unknown = set(self.availability_columns) - set(self.time_columns)
        if unknown:
            raise SpecError(f"availability_columns for tables without a time column: {unknown}")
        if self.label_table in self.static_tables:
            raise SpecError(
                "label_table must not also be declared static (it may be temporal, with a "
                "time_columns entry, when it doubles as history)"
            )
        overlap = set(self.static_tables) & set(self.time_columns)
        if overlap:
            raise SpecError(f"tables declared both static and temporal: {overlap}")
        unknown_unit = set(self.time_units) - set(self.time_columns)
        if unknown_unit:
            raise SpecError(f"time_units names tables without a time column: {unknown_unit}")
        bad_unit = {t: u for t, u in self.time_units.items() if u not in TIME_UNITS}
        if bad_unit:
            raise SpecError(
                f"time_units must be one of {', '.join(TIME_UNITS[:-1])} or {TIME_UNITS[-1]}, "
                f"got {bad_unit}"
            )
        self._check_validity_and_keys()
        if not (self.label_columns or self.output_roles.label):
            raise SpecError("name the label: label_columns or output_roles.label (or both)")
        self._check_output_roles()

    def _check_validity_and_keys(self) -> None:
        for table, column in self.validity_end_columns.items():
            if table not in self.time_columns:
                raise SpecError(
                    f"validity_end_columns names a table without a time column: {table}"
                )
            if column in (self.time_columns[table], self.availability_columns.get(table)):
                raise SpecError(
                    f"validity_end_columns[{table}] is already the time or availability column"
                )
        classified = {
            self.label_table,
            *self.time_columns,
            *self.static_tables,
            *self.outcome_tables,
        }
        unknown = set(self.unique_keys) - classified
        if unknown:
            raise SpecError(
                f"unique_keys names tables the spec does not classify: {sorted(unknown)}"
            )

    def _check_output_roles(self) -> None:
        roles = self.output_roles
        lists = {"label": roles.label, "metadata": roles.metadata, "features": roles.features or ()}
        seen: set[str] = set()
        for column in (c for names in lists.values() for c in names):
            if column in seen:
                raise SpecError(f"output_roles lists {column!r} in more than one role")
            seen.add(column)
        identity = {*self.entity_keys, self.prediction_ts} & seen
        if identity:
            raise SpecError(
                "output_roles may not name the entity key or the prediction time: "
                f"{sorted(identity)}"
            )

    @property
    def outcome_tables(self) -> set[str]:
        return {c.table for c in self.outcome_columns}

    @property
    def outcome_only_tables(self) -> set[str]:
        """Tables that hold outcome columns and nothing else the spec classifies.

        A label, temporal or static table with an outcome column is still read for its other
        columns, so only the outcome columns themselves are the target there.
        """
        classified = {self.label_table, *self.time_columns, *self.static_tables}
        return self.outcome_tables - classified

    @property
    def passthrough_columns(self) -> set[str]:
        """Output columns that identify the row or carry the target, not features."""
        return {
            *self.entity_keys,
            self.prediction_ts,
            *self.label_columns,
            *self.output_roles.label,
        }

    @property
    def non_feature_outputs(self) -> set[str]:
        """Output columns declared as label or metadata (see `is_non_feature_output`)."""
        return {*self.output_roles.label, *self.output_roles.metadata}

    def is_non_feature_output(self, column: str) -> bool:
        """Whether the spec says this output column is not a feature: declared as label or
        metadata, or absent from the `features` list when one is given."""
        column = column.lower()
        if column in self.non_feature_outputs:
            return True
        features = self.output_roles.features
        return features is not None and column not in features

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
        roles = OutputRoles.from_value(data.get("output_roles"))
        missing = [key for key in ("label_table", "prediction_ts") if not data.get(key)]
        if data.get("entity_keys") is None:
            missing.append("entity_keys (write [] for a single series)")
        if not data.get("label_columns") and not roles.label:
            missing.append("label_columns (or output_roles.label)")
        if not data.get("time_columns"):
            missing.append("time_columns")
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
            entity_keys=_names(data["entity_keys"], "entity_keys", allow_empty=True),
            prediction_ts=_name(data["prediction_ts"], "prediction_ts"),
            label_columns=_names(
                data.get("label_columns") or [], "label_columns", allow_empty=True
            ),
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
                else parse_duration(data["label_known_after"], "label_known_after", allow_zero=True)
            ),
            relations=_relations(data.get("relations")),
            time_units=_time_units(data.get("time_units")),
            output_roles=roles,
            validity_end_columns=_validity_end_columns(data.get("validity_end_columns")),
            unique_keys=_unique_keys(data.get("unique_keys")),
        )

    @classmethod
    def from_yaml(cls, path: Path | str) -> LeakageSpec:
        with open(path, encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
        if not isinstance(data, dict):
            raise SpecError(f"{path}: expected a mapping at the top level")
        return cls.from_dict(data)
