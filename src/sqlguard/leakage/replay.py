"""Point-in-time replay over Iceberg snapshot history.

For a sample of prediction timestamps `t` taken from the query's own output, every non-label
table is pinned to the snapshot that was visible strictly before `t`, the label spine is
restricted to rows at `t`, and the query is re-run. Values that differ from the normal
(current-data) run were not knowable at prediction time: that is leakage, whatever SQL
pattern caused it.

This is what catches late-arriving data (L4): the SQL can look perfectly time-correct while
the source table still received rows for the past after the prediction was made.

A PASS needs evidence: at least `min_rows` compared rows spread over `min_points` prediction
times. A query that keeps no rows, or too few, is UNVERIFIABLE rather than a pass (flagged
`insufficient_evidence`; the validator only lets that block a query that has a static
ingestion-time hint, which replay is the only way to settle).

A table that did not exist yet at the sampled time (its first main-branch snapshot became
visible later, as with a table loaded once from a seed or a migration) cannot be reconstructed:
the replay is UNVERIFIABLE, never a mismatch. Static tables are time-invariant by declaration and
are read as they are now. A snapshot becomes visible at its `sqlguard.logical-ts` property when
it has one, otherwise at its commit time.

Relation names of one to three parts (`events`, `db.events`, `"db"."main"."events"`) are mapped
to catalog tables (the spec's `relations`, or the catalog namespace) and exposed to the query
under the names it uses. An uncorrelated `(SELECT MAX(label_ts) FROM labels)` cutoff reads the
whole label table in both runs, since a time split is chosen before any prediction time exists.

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

from sqlguard.core.catalog import HistoryGap, LakehouseCatalog, naive_utc
from sqlguard.core.duck import connect
from sqlguard.core.parsing import parse_single
from sqlguard.leakage.analysis import analyze
from sqlguard.leakage.spec import LeakageSpec

_STRICT = timedelta(microseconds=1)
FULL_LABELS = "__labels_all"  # the whole label table, for uncorrelated cutoff scans
# A PASS must rest on enough evidence: a query that keeps few rows, or rows of one or two
# prediction times, proves nothing about the other times it will run for.
MIN_ROWS = 20
MIN_POINTS = 3
_ROW = "<row>"  # pseudo-column for rows present in only one of the two runs


class RelationError(LookupError):
    """A relation name in the query does not map to a table of the catalog."""


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
    timestamps_compared: int = 0  # distinct prediction times that had rows to compare
    # unverifiable only because too little was compared (not because replay could not run)
    insufficient_evidence: bool = False

    @property
    def mismatch_rate(self) -> float:
        return self.rows_mismatched / self.rows_compared if self.rows_compared else 0.0


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _normalize(value: object) -> object:
    """Compare floats to 9 significant digits (relative), and NaN equal to NaN."""
    if isinstance(value, float):
        return "nan" if math.isnan(value) else float(f"{value:.9g}")
    return value


def sample_points(stamps: Counter, max_points: int, seed: int) -> list[datetime]:
    """Pick up to `max_points` prediction times.

    Timestamps shared by several rows come first (up to half the budget): batch-aligned
    instants such as midnights are where boundary leaks concentrate. The rest of the budget is
    spread evenly over the time range, one seeded random pick per stratum, so that when every
    timestamp is unique the sample covers old and recent predictions alike.
    """
    if len(stamps) <= max_points:
        return sorted(stamps)
    shared = sorted((ts for ts, n in stamps.items() if n > 1), key=lambda ts: (-stamps[ts], ts))
    chosen = shared[: max_points // 2]
    rest = sorted(set(stamps) - set(chosen))
    budget = max_points - len(chosen)
    rng = random.Random(seed)
    for i in range(budget):  # stratum i of `budget` equal slices of the remaining timestamps
        low, high = i * len(rest) // budget, (i + 1) * len(rest) // budget
        chosen.append(rest[rng.randrange(low, high)])
    return sorted(chosen)


class Replayer:
    """Replays many queries against one catalog, caching pinned connections per timestamp."""

    def __init__(
        self,
        catalog: LakehouseCatalog,
        spec: LeakageSpec,
        max_points: int = 24,
        seed: int = 0,
        min_rows: int = MIN_ROWS,
        min_points: int = MIN_POINTS,
    ) -> None:
        self.catalog = catalog
        self.spec = spec
        self.max_points, self.seed = max_points, seed
        self.min_rows, self.min_points = min_rows, min_points
        self.tables = catalog.table_names()
        self.labels = catalog.read(spec.label_table)
        counts = Counter(self.labels.column(spec.prediction_ts).to_pylist())
        # every label timestamp, in the label column's own representation, by naive-UTC value
        self._label_stamps = {naive_utc(ts): ts for ts in counts if isinstance(ts, datetime)}
        # a query-independent sample over all labels (the point set `run` draws from its output)
        self.points: list[datetime] = sample_points(counts, max_points, seed)
        self._current: duckdb.DuckDBPyConnection | None = None
        self._pinned: dict[tuple[datetime, bool], duckdb.DuckDBPyConnection | HistoryGap] = {}
        # tables whose history starts after the pinned time (read as empty), by (time, history)
        self._late: dict[tuple[datetime, bool], dict[str, str]] = {}
        self._reads: dict[tuple[str, int | None], pa.Table] = {}
        self._schema: dict | None = None
        self._bound: dict[int, set[tuple[str, ...]]] = defaultdict(set)  # views made per connection

    def _read(self, table: str, snapshot_id: int | None) -> pa.Table:
        key = (table, snapshot_id)
        if key not in self._reads:
            if snapshot_id is None:
                schema = self.catalog.table(table).schema().as_arrow()
                self._reads[key] = schema.empty_table()
            else:
                self._reads[key] = self.catalog.read(table, snapshot_id)
        return self._reads[key]

    def _current_data(self, table: str) -> pa.Table:
        snapshot = self.catalog.table(table).current_snapshot()
        return self._read(table, snapshot.snapshot_id if snapshot else None)

    def current(self) -> duckdb.DuckDBPyConnection:
        if self._current is None:
            data = {t: self.catalog.read(t) for t in self.tables}
            data[FULL_LABELS] = self.labels
            self._current = connect(data)
        return self._current

    def _table_at(self, table: str, ts: datetime, before: datetime, late: dict[str, str]):
        """The rows of `table` as they stood just before `ts` (see the module docstring)."""
        if table in self.spec.static_tables:
            return self._current_data(table)
        snapshot = self.catalog.snapshot_as_of(table, before, strict_history=True)
        # an outcome table is unknown before the outcome by definition: empty is the right answer
        starts_late = snapshot is None and table not in self.spec.outcome_tables
        if starts_late and self.catalog.table(table).current_snapshot() is not None:
            late[table] = self._starts_after(table)
        return self._read(table, snapshot)

    def _starts_after(self, table: str) -> str:
        first = min(self.catalog.snapshots(table), key=lambda s: s.visible_at)
        committed = datetime.fromtimestamp(first.timestamp_ms / 1000, tz=UTC).replace(tzinfo=None)
        source = "commit time" if first.visible_at == committed else "sqlguard.logical-ts"
        return (
            f"table history starts after prediction time: {table!r} first became visible at "
            f"{first.visible_at.isoformat()} ({source})"
        )

    def pinned(self, ts: datetime, history: bool = False) -> duckdb.DuckDBPyConnection:
        """Connection where every table looks as it did just before `ts`.

        The label table holds the rows at `ts`. With `history`, it also holds the earlier rows
        with their label columns set to NULL: that those predictions exist, and when, was known
        at `ts`, but their outcomes may not have been. Features over the label table's own
        history (prior-prediction counts, time since the last prediction) therefore replay
        identically, while one that reads an earlier label's value does not. Labels that the
        spec's `label_known_after` says were final before `ts` keep their values.
        """
        key = (ts, history)
        if key not in self._pinned:
            try:
                inclusive = self.spec.boundary == "inclusive"
                before = naive_utc(ts) - (timedelta(0) if inclusive else _STRICT)
                late: dict[str, str] = {}
                data = {
                    t: self._table_at(t, ts, before, late)
                    for t in self.tables
                    if t != self.spec.label_table
                }
                data[self.spec.label_table] = self._pinned_labels(ts, history)
                data[FULL_LABELS] = self.labels
                self._late[key] = late
                self._pinned[key] = connect(data)
            except HistoryGap as err:
                self._pinned[key] = err
        entry = self._pinned[key]
        if isinstance(entry, HistoryGap):
            raise entry
        return entry

    def _pinned_labels(self, ts: datetime, history: bool) -> pa.Table:
        column = self.labels.column(self.spec.prediction_ts)
        at = self.labels.filter(pc.equal(column, pa.scalar(ts, type=column.type)))
        if not history:
            return at
        earlier = self.labels.filter(pc.less(column, pa.scalar(ts, type=column.type)))
        if self.spec.label_known_after:
            cutoff = pa.scalar(ts - timedelta(seconds=self.spec.label_known_after), column.type)
            stamped = earlier.column(self.spec.prediction_ts)
            # final once label_ts + known_after is before ts (at ts too, when inclusive)
            inclusive = self.spec.boundary == "inclusive"
            unknown = (
                pc.greater(stamped, cutoff) if inclusive else pc.greater_equal(stamped, cutoff)
            )
        else:
            unknown = pa.scalar(True)
        for name in self.spec.label_columns:
            index = earlier.schema.get_field_index(name)
            if index >= 0:
                field_ = earlier.schema.field(index)
                nulls = pa.scalar(None, field_.type)
                hidden = pc.if_else(unknown, nulls, earlier.column(index))
                earlier = earlier.set_column(index, field_, hidden)
        return pa.concat_tables([earlier, at])

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

    def _relation_target(self, parts: tuple[str, ...]) -> str:
        """The catalog table a qualified relation name stands for."""
        mapped = self.spec.relations.get(".".join(parts))
        if mapped is not None:
            target = mapped.split(".")
            if len(target) == 2 and target[0] == self.catalog.namespace.lower():
                target = target[1:]
            if len(target) == 1 and target[0] in self.tables:
                return target[0]
            raise RelationError(
                f"relation {'.'.join(parts)!r} is mapped to {mapped!r}, which is "
                f"not a table of namespace {self.catalog.namespace!r}"
            )
        if parts[-2] == self.catalog.namespace.lower() and parts[-1] in self.tables:
            return parts[-1]
        raise RelationError(
            f"relation {'.'.join(parts)!r} is not in namespace {self.catalog.namespace!r}; "
            "map it to a catalog table in the spec's `relations`"
        )

    def _relations(self, ast: exp.Expression) -> dict[tuple[str, ...], str]:
        """Relation names the query reads -> catalog tables (bare names map to themselves)."""
        ctes = {c.alias_or_name.lower() for c in ast.find_all(exp.CTE)}
        found: dict[tuple[str, ...], str] = {}
        for table in ast.find_all(exp.Table):
            if not table.name:
                continue  # a table function such as read_csv(): the sandbox refuses it
            parts = tuple(p.lower() for p in (table.catalog, table.db, table.name) if p)
            if len(parts) == 1:
                if parts[0] not in ctes:
                    found[parts] = parts[0]
            elif parts not in found:
                found[parts] = self._relation_target(parts)
        return found

    def _bind(self, conn: duckdb.DuckDBPyConnection, relations: dict[tuple[str, ...], str]) -> None:
        """Expose each qualified relation under the name the query uses (views over the tables)."""
        done = self._bound[id(conn)]
        for parts, target in relations.items():
            if len(parts) == 1 or parts in done:
                continue
            quoted = [_quote(p) for p in parts]
            if len(parts) == 3 and parts[0] != "memory":
                attached = {
                    row[0]
                    for row in conn.execute(
                        "SELECT database_name FROM duckdb_databases()"
                    ).fetchall()
                }
                if parts[0] not in attached:
                    conn.execute(f"ATTACH ':memory:' AS {quoted[0]}")
            conn.execute(f"CREATE SCHEMA IF NOT EXISTS {'.'.join(quoted[:-1])}")
            source = f"temp.main.{_quote(target)}"
            conn.execute(f"CREATE OR REPLACE VIEW {'.'.join(quoted)} AS SELECT * FROM {source}")
            done.add(parts)

    def _keep_label_cutoffs(self, sql: str) -> str:
        """Point uncorrelated cutoff scans of the label table at the whole table."""
        if self._schema is None:
            self._schema = self.catalog.schema_for()
        try:
            analysis = analyze(sql, self.spec, self._schema)
        except Exception:  # noqa: BLE001 - the original SQL is replayed as written
            return sql
        if not analysis.global_scans:
            return sql
        for ref in analysis.global_scans:
            ref.node.set("this", exp.to_identifier(FULL_LABELS))
            ref.node.set("db", None)
            ref.node.set("catalog", None)
        return analysis.qualified.sql(dialect=self.spec.dialect)

    def run(self, sql: str) -> ReplayResult:
        spec = self.spec
        try:
            ast = parse_single(sql, spec.dialect)
        except ValueError as err:
            return ReplayResult(ReplayStatus.UNVERIFIABLE, reason=str(err))
        if not isinstance(ast, exp.Query):
            return ReplayResult(ReplayStatus.UNVERIFIABLE, reason="only SELECT queries replay")
        try:
            relations = self._relations(ast)
        except RelationError as err:
            return ReplayResult(ReplayStatus.UNVERIFIABLE, reason=str(err))
        read = set(relations.values())
        if spec.label_table not in read:
            return ReplayResult(ReplayStatus.UNVERIFIABLE, reason="label table not referenced")
        sql = self._keep_label_cutoffs(sql)
        try:
            self._bind(self.current(), relations)
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
        full_by_ts: dict[datetime, dict[tuple, list[tuple]]] = defaultdict(dict)
        for key, rows in self._rows_by_key(full, key_cols, value_cols).items():
            if isinstance(key[-1], datetime):
                full_by_ts[naive_utc(key[-1])][key] = rows

        # Replay the prediction times the query actually produced, not arbitrary label times.
        produced = Counter(
            {ts: sum(len(r) for r in rows.values()) for ts, rows in full_by_ts.items()}
        )
        usable = Counter({ts: n for ts, n in produced.items() if ts in self._label_stamps})
        points = [
            self._label_stamps[ts] for ts in sample_points(usable, self.max_points, self.seed)
        ]
        if not points:
            return ReplayResult(
                ReplayStatus.UNVERIFIABLE,
                reason="no label prediction time appears in the query output",
                insufficient_evidence=True,
            )

        compared = mismatched = timestamps = 0
        columns: Counter[str] = Counter()
        for ts in points:
            try:
                conn = self.pinned(ts, history=True)
                late = sorted(read & self._late[(ts, True)].keys())
                if late:
                    return ReplayResult(
                        ReplayStatus.UNVERIFIABLE, reason=self._late[(ts, True)][late[0]]
                    )
                self._bind(conn, relations)
                replayed = conn.sql(sql).to_arrow_table()
            except HistoryGap as err:
                return ReplayResult(ReplayStatus.UNVERIFIABLE, reason=str(err))
            except duckdb.Error as err:
                return ReplayResult(ReplayStatus.UNVERIFIABLE, reason=f"replay failed: {err}")
            # earlier prediction rows are context only: compare the rows at `ts`
            past_rows = {
                key: rows
                for key, rows in self._rows_by_key(replayed, key_cols, value_cols).items()
                if isinstance(key[-1], datetime) and naive_utc(key[-1]) == naive_utc(ts)
            }
            now_rows = full_by_ts.get(naive_utc(ts), {})
            keys = now_rows.keys() | past_rows.keys()
            timestamps += bool(keys)
            for key in keys:
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

        def result(status: ReplayStatus, reason: str = "") -> ReplayResult:
            return ReplayResult(
                status,
                len(points),
                compared,
                mismatched,
                dict(columns),
                reason,
                timestamps,
                insufficient_evidence=status is ReplayStatus.UNVERIFIABLE,
            )

        if mismatched:
            return result(ReplayStatus.MISMATCH)
        if compared < self.min_rows or timestamps < self.min_points:
            return result(
                ReplayStatus.UNVERIFIABLE,
                f"replay compared {compared} row(s) at {timestamps} prediction time(s); a pass "
                f"needs at least {self.min_rows} rows at {self.min_points} times",
            )
        return result(ReplayStatus.PASS)
