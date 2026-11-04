"""Point-in-time replay over Iceberg snapshot history.

For a sample of prediction timestamps `t`, every non-label table is pinned to the snapshot
that was visible strictly before `t`, the label spine is restricted to rows at `t`, and the
query is re-run. Feature values that differ from the normal (current-data) run were not
knowable at prediction time: that is leakage, whatever SQL pattern caused it.

This is what catches late-arriving data (L4): the SQL can look perfectly time-correct while
the source table still received rows for the past after the prediction was made.
"""

from __future__ import annotations

import random
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum

import duckdb
import pyarrow as pa
import pyarrow.compute as pc
from sqlglot import exp

from sqlguard.core.catalog import LakehouseCatalog
from sqlguard.core.duck import connect
from sqlguard.core.parsing import parse_single
from sqlguard.leakage.spec import LeakageSpec

_STRICT = timedelta(microseconds=1)
_ROW = "<row>"  # pseudo-column for rows present in only one of the two runs


class ReplayStatus(StrEnum):
    PASS = "pass"
    MISMATCH = "mismatch"
    UNVERIFIABLE = "unverifiable"


@dataclass(frozen=True)
class ReplayResult:
    status: ReplayStatus
    points_checked: int = 0
    rows_compared: int = 0
    rows_mismatched: int = 0
    mismatched_columns: dict[str, int] = field(default_factory=dict)
    reason: str = ""

    @property
    def mismatch_rate(self) -> float:
        return self.rows_mismatched / self.rows_compared if self.rows_compared else 0.0


class Unverifiable(Exception):
    pass


def _normalize(value: object) -> object:
    return round(value, 6) if isinstance(value, float) else value


class Replayer:
    """Replays many queries against one catalog, caching pinned connections per timestamp."""

    def __init__(
        self, catalog: LakehouseCatalog, spec: LeakageSpec, max_points: int = 24, seed: int = 0
    ) -> None:
        self.catalog = catalog
        self.spec = spec
        self.tables = catalog.table_names()
        self.labels = catalog.read(spec.label_table)
        stamps = sorted(set(self.labels.column(spec.prediction_ts).to_pylist()))
        if len(stamps) > max_points:
            stamps = sorted(random.Random(seed).sample(stamps, max_points))
        self.points: list[datetime] = stamps
        self._current: duckdb.DuckDBPyConnection | None = None
        self._pinned: dict[datetime, duckdb.DuckDBPyConnection | Unverifiable] = {}
        self._reads: dict[tuple[str, int | None], pa.Table] = {}

    # -- connections -------------------------------------------------------------------
    def _read(self, table: str, snapshot_id: int | None) -> pa.Table:
        key = (table, snapshot_id)
        if key not in self._reads:
            if snapshot_id is None:
                schema = self.catalog.table(table).schema().as_arrow()
                self._reads[key] = schema.empty_table()
            else:
                self._reads[key] = self.catalog.read(table, snapshot_id)
        return self._reads[key]

    def _snapshot_before(self, table: str, ts: datetime) -> int | None:
        snapshot_id = self.catalog.snapshot_as_of(table, ts - _STRICT)
        if snapshot_id is None:
            snapshots = self.catalog.snapshots(table)
            if snapshots and self.catalog.history_truncated(table):
                raise Unverifiable(
                    f"history of {table!r} before {ts.isoformat()} was expired; "
                    "the state at prediction time can no longer be reconstructed"
                )
        return snapshot_id

    def current(self) -> duckdb.DuckDBPyConnection:
        if self._current is None:
            self._current = connect({t: self.catalog.read(t) for t in self.tables})
        return self._current

    def pinned(self, ts: datetime) -> duckdb.DuckDBPyConnection:
        if ts not in self._pinned:
            try:
                data = {
                    t: self._read(t, self._snapshot_before(t, ts))
                    for t in self.tables
                    if t != self.spec.label_table
                }
                spine = self.labels.filter(
                    pc.equal(self.labels.column(self.spec.prediction_ts), pa.scalar(ts))
                )
                data[self.spec.label_table] = spine
                self._pinned[ts] = connect(data)
            except Unverifiable as err:
                self._pinned[ts] = err
        entry = self._pinned[ts]
        if isinstance(entry, Unverifiable):
            raise entry
        return entry

    # -- replay ------------------------------------------------------------------------
    def _rows_by_key(
        self, table: pa.Table, key_cols: list[str], feature_cols: list[str]
    ) -> dict[tuple, list[tuple]]:
        grouped: dict[tuple, list[tuple]] = defaultdict(list)
        for row in table.to_pylist():
            key = tuple(row[c] for c in key_cols)
            grouped[key].append(tuple(_normalize(row[c]) for c in feature_cols))
        for rows in grouped.values():
            rows.sort(key=repr)
        return grouped

    def run(self, sql: str) -> ReplayResult:
        spec = self.spec
        try:
            ast = parse_single(sql, spec.dialect)
        except ValueError as err:
            return ReplayResult(ReplayStatus.UNVERIFIABLE, reason=str(err))
        referenced = {t.name for t in ast.find_all(exp.Table)}
        if spec.label_table not in referenced:
            return ReplayResult(ReplayStatus.UNVERIFIABLE, reason="label table not referenced")
        try:
            full = self.current().sql(sql).to_arrow_table()
        except duckdb.Error as err:
            return ReplayResult(ReplayStatus.UNVERIFIABLE, reason=f"query failed: {err}")
        key_cols = [*spec.entity_keys, spec.prediction_ts]
        missing = [c for c in key_cols if c not in full.column_names]
        if missing:
            return ReplayResult(
                ReplayStatus.UNVERIFIABLE,
                reason=f"output must include {missing} to align rows for replay",
            )
        feature_cols = [c for c in full.column_names if c not in spec.passthrough_columns]
        full_rows = self._rows_by_key(full, key_cols, feature_cols)

        compared = mismatched = 0
        columns: Counter[str] = Counter()
        for ts in self.points:
            try:
                replayed = self.pinned(ts).sql(sql).to_arrow_table()
            except Unverifiable as err:
                return ReplayResult(ReplayStatus.UNVERIFIABLE, reason=str(err))
            except duckdb.Error as err:
                return ReplayResult(ReplayStatus.UNVERIFIABLE, reason=f"replay failed: {err}")
            past_rows = self._rows_by_key(replayed, key_cols, feature_cols)
            now_rows = {k: v for k, v in full_rows.items() if k[-1] == ts}
            for key in now_rows.keys() | past_rows.keys():
                compared += 1
                now, past = now_rows.get(key), past_rows.get(key)
                if now == past:
                    continue
                mismatched += 1
                if now is None or past is None or len(now) != len(past):
                    columns[_ROW] += 1
                    continue
                for i, col in enumerate(feature_cols):
                    if any(a[i] != b[i] for a, b in zip(now, past, strict=True)):
                        columns[col] += 1
        status = ReplayStatus.MISMATCH if mismatched else ReplayStatus.PASS
        return ReplayResult(status, len(self.points), compared, mismatched, dict(columns))
