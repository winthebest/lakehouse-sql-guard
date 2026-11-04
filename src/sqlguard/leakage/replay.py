"""Point-in-time replay over Iceberg snapshot history.

For a sample of prediction timestamps `t`, every non-label table is pinned to the snapshot
that was visible strictly before `t`, the label spine is restricted to rows at `t`, and the
query is re-run. Values that differ from the normal (current-data) run were not knowable at
prediction time: that is leakage, whatever SQL pattern caused it.

This is what catches late-arriving data (L4): the SQL can look perfectly time-correct while
the source table still received rows for the past after the prediction was made.

User SQL runs in a sandboxed in-memory DuckDB (no file or network access), and only SELECT
queries are replayed.
"""

from __future__ import annotations

import math
import random
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum

import duckdb
import pyarrow as pa
import pyarrow.compute as pc
from sqlglot import exp

from sqlguard.core.catalog import HistoryGap, LakehouseCatalog
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


def _normalize(value: object) -> object:
    """Compare floats to 9 significant digits (relative), and NaN equal to NaN."""
    if isinstance(value, float):
        return "nan" if math.isnan(value) else float(f"{value:.9g}")
    return value


def _naive_utc(ts: datetime) -> datetime:
    return ts.astimezone(UTC).replace(tzinfo=None) if ts.tzinfo else ts


def sample_points(stamps: Counter, max_points: int, seed: int) -> list[datetime]:
    """Half the budget goes to the busiest timestamps (where boundary leaks concentrate,
    e.g. batch-aligned midnights), the rest is a seeded random sample of the others."""
    if len(stamps) <= max_points:
        return sorted(stamps)
    busiest = [ts for ts, _ in sorted(stamps.items(), key=lambda kv: (-kv[1], kv[0]))]
    chosen = busiest[: max_points // 2]
    rest = sorted(set(stamps) - set(chosen))
    chosen += random.Random(seed).sample(rest, max_points - len(chosen))
    return sorted(chosen)


class Replayer:
    """Replays many queries against one catalog, caching pinned connections per timestamp."""

    def __init__(
        self, catalog: LakehouseCatalog, spec: LeakageSpec, max_points: int = 24, seed: int = 0
    ) -> None:
        self.catalog = catalog
        self.spec = spec
        self.tables = catalog.table_names()
        self.labels = catalog.read(spec.label_table)
        counts = Counter(self.labels.column(spec.prediction_ts).to_pylist())
        self.points: list[datetime] = sample_points(counts, max_points, seed)
        self._current: duckdb.DuckDBPyConnection | None = None
        self._pinned: dict[datetime, duckdb.DuckDBPyConnection | HistoryGap] = {}
        self._reads: dict[tuple[str, int | None], pa.Table] = {}

    def _read(self, table: str, snapshot_id: int | None) -> pa.Table:
        key = (table, snapshot_id)
        if key not in self._reads:
            if snapshot_id is None:
                schema = self.catalog.table(table).schema().as_arrow()
                self._reads[key] = schema.empty_table()
            else:
                self._reads[key] = self.catalog.read(table, snapshot_id)
        return self._reads[key]

    def current(self) -> duckdb.DuckDBPyConnection:
        if self._current is None:
            self._current = connect({t: self.catalog.read(t) for t in self.tables})
        return self._current

    def pinned(self, ts: datetime) -> duckdb.DuckDBPyConnection:
        """Connection where every table looks as it did just before `ts`."""
        if ts not in self._pinned:
            try:
                before = _naive_utc(ts) - _STRICT
                data = {
                    t: self._read(t, self.catalog.snapshot_as_of(t, before, strict_history=True))
                    for t in self.tables
                    if t != self.spec.label_table
                }
                column = self.labels.column(self.spec.prediction_ts)
                data[self.spec.label_table] = self.labels.filter(
                    pc.equal(column, pa.scalar(ts, type=column.type))
                )
                self._pinned[ts] = connect(data)
            except HistoryGap as err:
                self._pinned[ts] = err
        entry = self._pinned[ts]
        if isinstance(entry, HistoryGap):
            raise entry
        return entry

    def _rows_by_key(
        self, table: pa.Table, key_cols: list[str], value_cols: list[str]
    ) -> dict[tuple, list[tuple]]:
        grouped: dict[tuple, list[tuple]] = defaultdict(list)
        for row in table.to_pylist():
            key = tuple(row[c] for c in key_cols)
            grouped[key].append(tuple(_normalize(row[c]) for c in value_cols))
        for rows in grouped.values():
            rows.sort(key=repr)
        return grouped

    def run(self, sql: str) -> ReplayResult:
        spec = self.spec
        try:
            ast = parse_single(sql, spec.dialect)
        except ValueError as err:
            return ReplayResult(ReplayStatus.UNVERIFIABLE, reason=str(err))
        if not isinstance(ast, exp.Query):
            return ReplayResult(ReplayStatus.UNVERIFIABLE, reason="only SELECT queries replay")
        if spec.label_table not in {t.name.lower() for t in ast.find_all(exp.Table)}:
            return ReplayResult(ReplayStatus.UNVERIFIABLE, reason="label table not referenced")
        try:
            full = self.current().sql(sql).to_arrow_table()
        except duckdb.Error as err:
            return ReplayResult(ReplayStatus.UNVERIFIABLE, reason=f"query failed: {err}")
        names = {c.lower(): c for c in full.column_names}
        missing = [c for c in spec.key_columns if c not in names]
        if missing:
            return ReplayResult(
                ReplayStatus.UNVERIFIABLE,
                reason=f"output must include {missing} to align rows for replay",
            )
        key_cols = [names[c] for c in spec.key_columns]
        # Every non-key column is compared, labels included: a "label" that changes between
        # runs is not the label.
        value_cols = [c for c in full.column_names if c not in key_cols]
        full_rows = self._rows_by_key(full, key_cols, value_cols)

        compared = mismatched = 0
        columns: Counter[str] = Counter()
        for ts in self.points:
            try:
                replayed = self.pinned(ts).sql(sql).to_arrow_table()
            except HistoryGap as err:
                return ReplayResult(ReplayStatus.UNVERIFIABLE, reason=str(err))
            except duckdb.Error as err:
                return ReplayResult(ReplayStatus.UNVERIFIABLE, reason=f"replay failed: {err}")
            past_rows = self._rows_by_key(replayed, key_cols, value_cols)
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
                for i, col in enumerate(value_cols):
                    if any(a[i] != b[i] for a, b in zip(now, past, strict=True)):
                        columns[col] += 1
        status = ReplayStatus.MISMATCH if mismatched else ReplayStatus.PASS
        return ReplayResult(status, len(self.points), compared, mismatched, dict(columns))
