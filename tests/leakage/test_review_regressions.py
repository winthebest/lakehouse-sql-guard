"""Regression tests for analysis gaps found in review: each query here was once misjudged."""

from datetime import UTC, datetime

import pyarrow as pa
import pytest

from sqlguard.core.catalog import LakehouseCatalog
from sqlguard.leakage.replay import Replayer, ReplayStatus, sample_points
from sqlguard.leakage.spec import LeakageSpec, SpecError
from sqlguard.leakage.validator import validate

from .conftest import SCHEMA, SPEC

SEL = "SELECT l.user_id, l.label_ts, l.churned"
AGG = f"{SEL}, SUM(e.amount) AS a FROM labels l JOIN events e ON e.user_id = l.user_id"


def verdict(sql: str, spec: LeakageSpec | None = None, schema=None) -> tuple[str, set[str]]:
    report = validate(sql, spec or LeakageSpec.from_dict(SPEC), schema=schema or SCHEMA)
    return report.verdict, {f.rule_id for f in report.findings}


@pytest.mark.parametrize(
    "sql, rule",
    [
        # identifiers are case-insensitive (unquoted SQL folds case)
        (
            f"{SEL}, SUM(E.AMOUNT) AS a FROM Labels l JOIN Events E ON E.user_id = l.user_id "
            "AND E.event_ts > l.label_ts GROUP BY 1, 2, 3",
            "L1",
        ),
        # a bound inside OR / NOT / CASE does not bound the join
        (f"{AGG} AND (e.ingested_at < l.label_ts OR e.amount > 0) GROUP BY 1, 2, 3", "L3"),
        (f"{AGG} AND NOT (e.ingested_at < l.label_ts) GROUP BY 1, 2, 3", "L3"),
        (
            f"{AGG} WHERE CASE WHEN e.ingested_at < l.label_ts THEN 1 ELSE 1 END = 1 "
            "GROUP BY 1, 2, 3",
            "L3",
        ),
        # a reused CTE must be bounded at each use
        (
            "WITH s AS (SELECT * FROM user_stats) "
            f"{SEL}, a.total_spend, b.n_orders FROM labels l "
            "JOIN s a ON a.user_id = l.user_id AND a.updated_at < l.label_ts "
            "JOIN s b ON b.user_id = l.user_id",
            "L2",
        ),
        # strict event-time bound does not excuse a forward-shifted ingestion bound
        (
            f"{AGG} AND e.event_ts < l.label_ts AND e.ingested_at < l.label_ts + INTERVAL 12 HOUR "
            "GROUP BY 1, 2, 3",
            "L5",
        ),
        # a bound against another copy of the label table is not the prediction time
        (
            f"{SEL}, SUM(e.amount) AS a FROM labels l JOIN labels l2 ON l2.user_id = l.user_id "
            "JOIN events e ON e.user_id = l.user_id AND e.ingested_at < l2.label_ts "
            "GROUP BY 1, 2, 3",
            "L3",
        ),
        # outcome reached through a predicate or a renamed column
        (
            f"{SEL}, o.days_to_churn AS churn_days FROM labels l JOIN user_outcomes o "
            "ON o.user_id = l.user_id",
            "L6",
        ),
        (
            f"{SEL}, EXISTS (SELECT 1 FROM user_outcomes o WHERE o.user_id = l.user_id) AS f "
            "FROM labels l",
            "L6",
        ),
        (f"{SEL}, CASE WHEN l.churned = 1 THEN 1 ELSE 0 END AS f FROM labels l", "L6"),
        # windows that look ahead or span the whole partition
        (
            f"{SEL}, LEAD(l.churned) OVER (PARTITION BY l.user_id ORDER BY l.label_ts) AS f "
            "FROM labels l",
            "L1",
        ),
        (
            f"{SEL}, SUM(e.amount) OVER (PARTITION BY e.user_id) AS f FROM labels l "
            "JOIN events e ON e.user_id = l.user_id AND e.event_ts > l.label_ts - INTERVAL 7 DAY",
            "L3",
        ),
        # dialect date functions and string intervals
        (f"{AGG} AND e.ingested_at < date_add(l.label_ts, INTERVAL 7 DAY) GROUP BY 1, 2, 3", "L1"),
        (f"{AGG} AND e.ingested_at < l.label_ts + INTERVAL '7 days' GROUP BY 1, 2, 3", "L1"),
        (f"{AGG} AND e.ingested_at < l.label_ts + INTERVAL '1 day 2 hours' GROUP BY 1, 2, 3", "L1"),
        (f"{AGG} AND e.ingested_at < l.label_ts + INTERVAL 30 MINUTE GROUP BY 1, 2, 3", "L5"),
        (
            f"{AGG} AND date_trunc('day', e.ingested_at) <= date_trunc('day', l.label_ts) "
            "GROUP BY 1, 2, 3",
            "L5",
        ),
    ],
)
def test_leaky_query_is_flagged(sql, rule):
    result, rules = verdict(sql)
    assert result == "leaky" and rule in rules, rules


@pytest.mark.parametrize(
    "sql",
    [
        f"{AGG} AND CAST(e.ingested_at AS TIMESTAMP) < l.label_ts GROUP BY 1, 2, 3",
        f"{AGG} AND e.ingested_at < l.label_ts + INTERVAL (-1) DAY GROUP BY 1, 2, 3",
        f"{AGG} AND e.ingested_at + INTERVAL 1 HOUR < l.label_ts GROUP BY 1, 2, 3",
        "WITH sp AS (SELECT user_id, label_ts, label_ts - INTERVAL 1 DAY AS cutoff, churned "
        "FROM labels) SELECT sp.user_id, sp.label_ts, sp.churned, MAX(s.total_spend) AS m "
        "FROM sp JOIN user_stats s ON s.user_id = sp.user_id AND s.updated_at < sp.cutoff "
        "GROUP BY 1, 2, 3",
    ],
)
def test_correct_query_stays_clean(sql):
    assert verdict(sql) == ("clean", set())


def test_snowflake_uppercase_identifiers():
    spec = LeakageSpec.from_dict({**SPEC, "dialect": "snowflake"})
    schema = {t.upper(): {c.upper(): ty for c, ty in cols.items()} for t, cols in SCHEMA.items()}
    sql = (
        "SELECT L.USER_ID, L.LABEL_TS, L.CHURNED, O.DAYS_TO_CHURN AS F FROM LABELS L "
        "JOIN USER_OUTCOMES O ON O.USER_ID = L.USER_ID"
    )
    assert verdict(sql, spec, schema)[0] == "leaky"
    sql = (
        "SELECT L.USER_ID, L.LABEL_TS, L.CHURNED, SUM(E.AMOUNT) AS A FROM LABELS L "
        "JOIN EVENTS E ON E.USER_ID = L.USER_ID AND E.INGESTED_AT < DATEADD(hour, -1, "
        "L.LABEL_TS) GROUP BY 1, 2, 3"
    )
    assert verdict(sql, spec, schema) == ("clean", set())


@pytest.mark.parametrize(
    "sql, rule",
    [
        ("COPY labels TO 'x.csv'", "PARSE"),
        ("INSERT INTO labels SELECT * FROM labels", "PARSE"),
        ("SELECT 1; SELECT 2", "PARSE"),
        ("SELECT m.user_id, m.x FROM misc m", "UNCLASSIFIED"),
        # prediction_ts in the output is not the label's own column
        (
            "SELECT l.user_id, s.total_spend AS label_ts, l.churned FROM labels l "
            "JOIN user_stats s ON s.user_id = l.user_id AND s.updated_at < l.label_ts",
            "SPEC",
        ),
    ],
)
def test_unchecked_input_is_never_clean(sql, rule):
    result, rules = verdict(sql)
    assert result in ("unverifiable", "leaky") and rule in rules


def test_spec_column_typo_is_unverifiable():
    spec = LeakageSpec.from_dict(
        {**SPEC, "time_columns": {**SPEC["time_columns"], "events": "event_tz"}}
    )
    result, rules = verdict(f"{AGG} AND e.ingested_at < l.label_ts GROUP BY 1, 2, 3", spec)
    assert result == "unverifiable" and "SPEC" in rules


def test_ingestion_hint_alone_needs_replay():
    sql = f"{AGG} AND e.event_ts < l.label_ts GROUP BY 1, 2, 3"
    report = validate(sql, LeakageSpec.from_dict(SPEC), schema=SCHEMA)
    assert report.leak_types == ["L4"]
    assert report.verdict == "unverifiable"  # L4 is below threshold; only replay can settle it


@pytest.mark.parametrize("field", ["entity_keys", "label_columns", "static_tables"])
def test_spec_rejects_string_where_list_expected(field):
    with pytest.raises(SpecError):
        LeakageSpec.from_dict({**SPEC, field: "user_id"})


def test_spec_names_are_case_folded():
    spec = LeakageSpec.from_dict({**SPEC, "label_table": "LABELS", "prediction_ts": "Label_TS"})
    assert spec.label_table == "labels" and spec.prediction_ts == "label_ts"
    assert spec.classify("Events") == "temporal"


def test_sampling_keeps_busiest_timestamps():
    busy = datetime(2026, 1, 10)
    stamps = {busy: 50, **{datetime(2026, 1, 1, h): 1 for h in range(24)}}
    from collections import Counter

    for seed in range(5):
        assert busy in sample_points(Counter(stamps), 6, seed)


@pytest.fixture
def tiny(tmp_path):
    catalog = LakehouseCatalog.local(tmp_path / "wh")
    catalog.create_table(
        "labels",
        pa.table(
            {
                "user_id": pa.array([1, 2], pa.int64()),
                "label_ts": pa.array([datetime(2026, 1, 5)] * 2, pa.timestamp("us")),
                "churned": pa.array([0, 1], pa.int64()),
            }
        ),
    )
    spec = LeakageSpec.from_dict(
        {
            "label_table": "labels",
            "entity_keys": ["user_id"],
            "prediction_ts": "label_ts",
            "label_columns": ["churned"],
            "time_columns": {"labels_x": "ts"},
        }
    )
    return catalog, spec


def test_replay_sandbox_blocks_file_access(tiny, tmp_path):
    catalog, spec = tiny
    target = tmp_path / "exfil.csv"
    replayer = Replayer(catalog, spec)
    copy = replayer.run(f"COPY labels TO '{target}'")
    assert copy.status is ReplayStatus.UNVERIFIABLE and not target.exists()
    read = replayer.run(
        "SELECT l.user_id, l.label_ts, l.churned FROM labels l, read_csv('/etc/hosts') h"
    )
    assert read.status is ReplayStatus.UNVERIFIABLE and "disabled" in read.reason


def test_replay_handles_tz_aware_prediction_time(tmp_path):
    catalog = LakehouseCatalog.local(tmp_path / "wh")
    tz = pa.timestamp("us", tz="UTC")
    when = datetime(2026, 1, 5, tzinfo=UTC)
    catalog.create_table(
        "labels",
        pa.table(
            {
                "user_id": pa.array([1], pa.int64()),
                "label_ts": pa.array([when], tz),
                "churned": pa.array([0], pa.int64()),
            }
        ),
    )
    spec = LeakageSpec.from_dict(
        {
            "label_table": "labels",
            "entity_keys": ["user_id"],
            "prediction_ts": "label_ts",
            "label_columns": ["churned"],
            "time_columns": {"other": "ts"},
        }
    )
    result = Replayer(catalog, spec).run("SELECT l.user_id, l.label_ts, l.churned FROM labels l")
    assert result.status is ReplayStatus.PASS


def test_tag_on_old_snapshot_does_not_hide_expiry(tmp_path):
    catalog = LakehouseCatalog.local(tmp_path / "wh")
    schema = pa.schema([("user_id", pa.int64()), ("v", pa.float64())])
    table = catalog.create_empty("stats", schema)
    for i in range(3):
        table.append(pa.table({"user_id": [i], "v": [float(i)]}, schema=schema))
    first = table.snapshots()[0].snapshot_id
    table.manage_snapshots().create_tag(first, "keep").commit()
    newest = table.current_snapshot().timestamp_ms
    table.maintenance.expire_snapshots().older_than(
        datetime.fromtimestamp(newest / 1000, tz=UTC)
    ).commit()
    assert catalog.history_truncated("stats")
