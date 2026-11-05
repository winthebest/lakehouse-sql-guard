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

A prediction time that precedes the first main-branch snapshot of a table the query reads (a table
loaded once from a seed or a migration) cannot be reconstructed and is left out; the others are
still compared, and the replay is UNVERIFIABLE, never a mismatch, only when too few remain. A table
declared static is pinned like any other (a mistaken declaration still shows up); only when its
history starts late is it read as it is now. A snapshot becomes visible at its
`sqlguard.logical-ts` property when it has one, otherwise at its commit time.

Relation names of one to three parts (`events`, `db.events`, `"db"."main"."events"`) are mapped
to catalog tables (the spec's `relations`, or the catalog name and namespace) and exposed to the
query under the names it uses. An uncorrelated `(SELECT MAX(label_ts) FROM labels)` cutoff reads the
whole label table in both runs, since a time split is chosen before any prediction time exists.

User SQL runs in a sandboxed in-memory DuckDB (no file or network access), and only SELECT
queries are replayed.
"""

from __future__ import annotations

import math
import random
from bisect import bisect_left
from collections import Counter, defaultdict
from collections.abc import Mapping
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
# A row in flight at a prediction time weighs (its age at that time / the longest delay) **
# TAIL_EXPONENT: a leak that a short margin leaves open is made of rows loaded just inside the
# longest delay, which are about one in a hundred, and a sum of young in-flight rows must not
# outweigh a single old one. A row of half the longest delay weighs 0.06, one at 90% weighs 0.66.
TAIL_EXPONENT = 4.0
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
    # Per table the query reads, summed over the compared prediction times: the rows of the
    # prediction rows' entities that the unpinned read has and the read pinned before the time
    # does not (rows absent from the pinned snapshot). A leak through that table could only have
    # shown in these rows, so a PASS with none of them says nothing about the table.
    # `exposed_points` counts the times that had any. Information only: no score depends on it.
    exposure: dict[str, int] = field(default_factory=dict)
    exposed_points: dict[str, int] = field(default_factory=dict)

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


def sample_points(
    stamps: Counter, max_points: int, seed: int, in_flight: Mapping[datetime, float] | None = None
) -> list[datetime]:
    """Pick up to `max_points` prediction times, plus the heaviest in-flight ones.

    Timestamps shared by several rows come first (up to half the budget): batch-aligned
    instants such as midnights are where boundary leaks concentrate. The rest of the budget is
    spread evenly over the time range, one seeded random pick per stratum, so that when every
    timestamp is unique the sample covers old and recent predictions alike.

    With `in_flight` (a weight per time for rows that already happened but are not loaded yet, see
    `Replayer._in_flight`) up to a third of `max_points` more times are added, the heaviest ones
    not already picked: a late-arrival leak shows only at a time that has such a row for a
    predicted entity, and the long delays that matter most are rare. They are added to the sample
    above, never taken from it, so what a replay without weights would have found it still finds.
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
    weights = in_flight or {}
    flying = sorted(
        (ts for ts in stamps if weights.get(ts, 0) > 0 and ts not in chosen),
        key=lambda ts: (-weights[ts], ts),
    )
    return sorted(chosen + flying[: max_points // 3])


MAX_FLIGHT_TIMES = 1024  # label times scored for rows in flight; more are thinned evenly


def _naive_micros(column: pa.ChunkedArray) -> pa.ChunkedArray:
    """A DATE or timestamp column (any unit, with or without a time zone) as naive-UTC
    `timestamp[us]`; anything else is not a time and raises."""
    kind = column.type
    if not (pa.types.is_date(kind) or pa.types.is_timestamp(kind)):
        raise TypeError(f"cannot read {kind} as a time")
    if pa.types.is_timestamp(kind) and kind.tz is not None:
        column = column.cast(pa.timestamp(kind.unit))  # the UTC wall time
    return column.cast(pa.timestamp("us"))


def flight_scores(
    data: pa.Table,
    time_column: str,
    availability: str,
    key: str | None,
    labels: pa.Table,
    prediction_ts: str,
    stamps: Mapping[datetime, datetime],
    max_times: int = MAX_FLIGHT_TIMES,
) -> dict[datetime, float]:
    """Weight, per label time, of the rows in flight then (see `Replayer._in_flight`).

    `stamps` maps each naive-UTC label time to the label column's own representation of it. At
    most `max_times` times are scored (an even, deterministic thinning of the sorted times). The
    rows are sorted by time once; a row in flight at `t` happened in `[t - longest delay, t)`, so
    each time looks at that slice only, found by binary search: the cost is O(n log n) once and
    O(log n + rows in the window) per time, not O(times x rows).
    """
    if data.num_rows == 0:
        return {}
    # both columns in one unit (microseconds, naive UTC), whatever their arrow types
    happened_all, loaded_all = (
        _naive_micros(data.column(time_column)),
        _naive_micros(data.column(availability)),
    )
    order = pc.sort_indices(happened_all)
    happened = happened_all.take(order).combine_chunks()
    loaded = loaded_all.take(order).combine_chunks()
    seconds = pc.divide(pc.cast(happened, pa.int64()), 10**6)
    longest = (
        pc.max(
            pc.divide(
                pc.subtract(pc.cast(loaded, pa.int64()), pc.cast(happened, pa.int64())), 10**6
            )
        ).as_py()
        or 0
    )
    if longest <= 0:
        return {}
    entities = data.column(key).take(order).combine_chunks() if key in data.column_names else None
    users = labels.column(key) if key in labels.column_names else None
    label_times = labels.column(prediction_ts)

    def first_at_or_after(moment: int) -> int:
        return bisect_left(range(len(seconds)), moment, key=lambda i: seconds[i].as_py())

    times = sorted(stamps)
    if len(times) > max_times:
        times = [times[i * len(times) // max_times] for i in range(max_times)]
    scores: dict[datetime, float] = {}
    for naive in times:
        original = stamps[naive]
        try:
            at = pa.scalar(naive, type=pa.timestamp("us"))
            moment = int(naive.replace(tzinfo=UTC).timestamp())
        except (pa.ArrowInvalid, pa.ArrowNotImplementedError, TypeError, ValueError):
            continue
        low, high = first_at_or_after(moment - longest), first_at_or_after(moment)
        if high <= low:
            continue
        window = slice(low, high)
        pending = pc.greater_equal(loaded.slice(low, high - low), at)
        if users is not None and entities is not None:
            predicted = users.filter(
                pc.equal(label_times, pa.scalar(original, type=label_times.type))
            )
            pending = pc.and_(
                pending, pc.is_in(entities.slice(low, high - low), value_set=predicted)
            )
        age = pc.subtract(pa.scalar(moment, pa.int64()), seconds[window])
        weight = pc.power(pc.divide(pc.cast(age, pa.float64()), float(longest)), TAIL_EXPONENT)
        total = pc.sum(pc.if_else(pending, weight, 0.0)).as_py() or 0.0
        if total > 0:
            scores[naive] = total
    return scores


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
        self._pinned_data: dict[tuple[datetime, bool], dict[str, pa.Table]] = {}
        # tables whose history starts after the pinned time (read as empty), by (time, history)
        self._late: dict[tuple[datetime, bool], dict[str, str]] = {}
        self._reads: dict[tuple[str, int | None], pa.Table] = {}
        self._predates: dict[tuple[str, datetime], bool] = {}
        self._schema: dict | None = None
        self._flying: dict[str, dict[datetime, float]] = {}
        self._flight_errors: dict[str, str] = {}  # why a table's in-flight scoring was skipped
        self._counts: dict[tuple, dict[tuple, int]] = {}
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
        snapshot = self.catalog.snapshot_as_of(table, before, strict_history=True)
        # an outcome table is unknown before the outcome by definition: empty is the right answer
        starts_late = snapshot is None and table not in self.spec.outcome_tables
        if starts_late and self.catalog.table(table).current_snapshot() is not None:
            if table in self.spec.static_tables:
                return self._current_data(table)  # no history to check the declaration against
            late[table] = self._starts_after(table)
        return self._read(table, snapshot)

    def _predates_history(self, table: str, ts: datetime) -> bool:
        """Does `ts` come before the first snapshot of `table` that a replay may rely on?"""
        if (
            table == self.spec.label_table
            or table in self.spec.outcome_tables
            or table in self.spec.static_tables
        ):
            return False  # empty-before-it-exists, read as it is now, or the label spine
        key = (table, ts)
        if key not in self._predates:
            inclusive = self.spec.boundary == "inclusive"
            before = naive_utc(ts) - (timedelta(0) if inclusive else _STRICT)
            try:
                snapshot = self.catalog.snapshot_as_of(table, before, strict_history=True)
            except HistoryGap:
                self._predates[key] = False  # expired history: sampling it reports the gap
            else:
                late = snapshot is None and self.catalog.table(table).current_snapshot() is not None
                self._predates[key] = late
        return self._predates[key]

    def _within_history(self, read: set[str], usable: Counter) -> tuple[Counter, int, str]:
        """Keep the prediction times that every table the query reads has history for.

        Times before a table's first snapshot cannot be reconstructed. Sampling from only the
        times that can be replayed, rather than sampling all of them and dropping the unlucky,
        puts the evidence budget where there is evidence to gather. Returns the kept times, how
        many were left out, and why.
        """
        stamps = sorted(usable)
        start, why = 0, ""
        if not stamps:
            return usable, 0, ""
        for table in sorted(read):
            if not self._predates_history(table, stamps[0]):
                continue  # the history reaches back past the first prediction time: all covered
            # being before the start of history is monotone in time: find the first covered time
            lo, hi = 0, len(stamps)
            while lo < hi:
                mid = (lo + hi) // 2
                if self._predates_history(table, stamps[mid]):
                    lo = mid + 1
                else:
                    hi = mid
            if lo > start:
                start, why = lo, self._starts_after(table)
        if not start:
            return usable, 0, ""
        return Counter({ts: usable[ts] for ts in stamps[start:]}), start, why

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
                self._pinned_data[key] = data
                self._pinned[key] = connect(data)
            except HistoryGap as err:
                self._pinned[key] = err
        entry = self._pinned[key]
        if isinstance(entry, HistoryGap):
            raise entry
        return entry

    def _in_flight(self, table: str) -> dict[datetime, float]:
        """Per label time `t` (naive UTC): how much of `table` had happened but was not yet loaded.

        For a table with a declared availability column, each row of an entity predicted at `t`
        whose time is before `t` and whose availability is at or after it counts
        `(age at t / longest delay) ** TAIL_EXPONENT`. A row is a risk for an event-time margin
        only while it is old and still unloaded, so the weight grows as the row nears its load
        time and the rare long delays dominate. Tables without an availability column, and times
        with nothing in flight, score zero.
        """
        if table not in self._flying:
            scores: dict[datetime, float] = {}
            availability = self.spec.availability_columns.get(table)
            time_column = self.spec.time_columns.get(table)
            key = self.spec.entity_keys[0] if len(self.spec.entity_keys) == 1 else None
            try:
                data = self._current_data(table) if table in self.tables else None
                if (
                    availability
                    and data is not None
                    and {time_column, availability} <= set(data.column_names)
                ):
                    scores = self._flight_scores(data, time_column, availability, key)
            except Exception as err:  # noqa: BLE001 - sampling help must never abort a replay
                self._flight_errors[table] = f"{type(err).__name__}: {err}"
                scores = {}
            self._flying[table] = scores
        return self._flying[table]

    def _flight_scores(
        self, data: pa.Table, time_column: str, availability: str, key: str | None
    ) -> dict[datetime, float]:
        return flight_scores(
            data,
            time_column,
            availability,
            key,
            self.labels,
            self.spec.prediction_ts,
            self._label_stamps,
        )

    def _exposed(self, table: str, ts: datetime, entities: set[tuple] | None) -> int:
        """Rows of `table` the current read has that the read pinned before `ts` lacks (see
        `ReplayResult.exposure`), for the given entity key tuples when the table has the keys.

        The pinned spine keeps the rows at `ts` itself, so for the label table these are the
        later rows only. A row that is stamped late but present in the pinned read is not hidden.
        """
        current = self.labels if table == self.spec.label_table else self._current_data(table)
        pinned = self._pinned_data[(ts, True)][table]
        keys = list(self.spec.entity_keys)
        if entities is None or not all(k in current.column_names for k in keys):
            return max(current.num_rows - pinned.num_rows, 0)
        now = self._entity_counts(("current", table), current, keys)
        then = self._entity_counts((ts, table), pinned, keys)
        return sum(max(now.get(e, 0) - then.get(e, 0), 0) for e in entities)

    def _entity_counts(self, key: tuple, table: pa.Table, keys: list[str]) -> dict[tuple, int]:
        """Rows per entity key tuple, cached by what the table is (current, or pinned at a time)."""
        if key not in self._counts:
            grouped = table.group_by(keys).aggregate([(keys[0], "count")])
            rows = zip(*(grouped.column(k).to_pylist() for k in keys), strict=True)
            self._counts[key] = dict(
                zip(rows, grouped.column(f"{keys[0]}_count").to_pylist(), strict=True)
            )
        return self._counts[key]

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
        catalog_name = str(self.catalog.catalog.name).lower()
        if len(parts) == 3 and parts[0] != catalog_name:
            raise RelationError(
                f"relation {'.'.join(parts)!r} names catalog {parts[0]!r}, not {catalog_name!r}; "
                "map it to a catalog table in the spec's `relations`"
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
        usable, excluded, excluded_why = self._within_history(read, usable)
        if excluded and not usable:
            return ReplayResult(ReplayStatus.UNVERIFIABLE, reason=excluded_why)
        flying = Counter()
        flight_notes = []
        for table in sorted(read):
            flying.update(self._in_flight(table))
            if table in self._flight_errors:  # fall back to the base sample, and say so
                flight_notes.append(
                    f"in-flight sampling skipped for {table!r}: {self._flight_errors[table]}"
                )
        points = [
            self._label_stamps[ts]
            for ts in sample_points(usable, self.max_points, self.seed, flying)
        ]
        if not points:
            return ReplayResult(
                ReplayStatus.UNVERIFIABLE,
                reason="no label prediction time appears in the query output",
                insufficient_evidence=True,
            )

        compared = mismatched = timestamps = 0
        columns: Counter[str] = Counter()
        exposure: Counter[str] = Counter()
        exposed_points: Counter[str] = Counter()
        skipped: list[str] = []  # why prediction times before a table's history were left out
        for ts in points:
            try:
                conn = self.pinned(ts, history=True)
                late = sorted(read & self._late[(ts, True)].keys())
                if late:  # this time predates a table's history: judge the others without it
                    skipped.append(self._late[(ts, True)][late[0]])
                    continue
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
            if keys:
                entities = {tuple(k[:-1]) for k in keys}
                for table in sorted(read & self._pinned_data[(ts, True)].keys()):
                    seen = self._exposed(table, ts, entities)
                    exposure[table] += seen
                    exposed_points[table] += bool(seen)
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

        if len(skipped) == len(points):
            return ReplayResult(ReplayStatus.UNVERIFIABLE, reason=skipped[0])
        notes = list(flight_notes)
        if skipped:
            notes.append(f"{len(skipped)} of {len(points)} point(s) skipped: {skipped[0]}")
        if excluded:
            notes.append(
                f"{excluded} prediction time(s) before the start of history left out, "
                f"{len(points)} sampled from the rest: {excluded_why}"
            )

        def result(
            status: ReplayStatus, reason: str = "", insufficient: bool | None = None
        ) -> ReplayResult:
            return ReplayResult(
                status,
                len(points) - len(skipped),
                compared,
                mismatched,
                dict(columns),
                "; ".join(filter(None, [reason, *notes])),
                timestamps,
                insufficient_evidence=(
                    status is ReplayStatus.UNVERIFIABLE if insufficient is None else insufficient
                ),
                exposure=dict(exposure),
                exposed_points=dict(exposed_points),
            )

        if mismatched:
            return result(ReplayStatus.MISMATCH)
        if compared < self.min_rows or timestamps < self.min_points:
            return result(
                ReplayStatus.UNVERIFIABLE,
                f"replay compared {compared} row(s) at {timestamps} prediction time(s); a pass "
                f"needs at least {self.min_rows} rows at {self.min_points} times",
                # Points left out for lack of history make this a gap in coverage, not just a
                # small sample: it must keep blocking, as a table with expired history does.
                insufficient=not (skipped or excluded),
            )
        return result(ReplayStatus.PASS)
