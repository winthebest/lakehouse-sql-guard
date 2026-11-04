"""Regression tests for analysis gaps found in review: each query here was once misjudged."""

from collections import Counter
from datetime import UTC, datetime, timedelta

import pyarrow as pa
import pytest

from bench.leakage.world import WorldConfig, build_world
from sqlguard.core.catalog import LOGICAL_TS_PROPERTY, LakehouseCatalog
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
    stamps = [datetime(2026, 1, 5 + day, tzinfo=UTC) for day in range(4) for _ in range(6)]
    catalog.create_table(
        "labels",
        pa.table(
            {
                "user_id": pa.array(range(len(stamps)), pa.int64()),
                "label_ts": pa.array(stamps, tz),
                "churned": pa.array([0] * len(stamps), pa.int64()),
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
    assert result.status is ReplayStatus.PASS and result.rows_compared == len(stamps)


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


# --- static shapes: leaks that must be caught, correct queries that must stay clean ------

STATS = "ASOF JOIN user_stats s ON s.user_id = l.user_id AND l.label_ts > s.updated_at"
FEAT = f"{SEL}, s.total_spend FROM labels l {STATS}"


def windowed(expr: str) -> str:
    """A feature CTE computing `expr` over user_stats, ASOF-joined to the spine."""
    return (
        f"WITH f AS (SELECT user_id, updated_at, {expr} AS v FROM user_stats) "
        f"{SEL}, f.v FROM labels l ASOF JOIN f ON f.user_id = l.user_id "
        "AND l.label_ts > f.updated_at"
    )


LEAKY_SHAPES = [
    # nested CTE consumed twice, once unbounded (id, sql, rule)
    (
        "nested cte reuse",
        "WITH a AS (SELECT * FROM user_stats), b AS (SELECT * FROM a) "
        f"{SEL}, x.total_spend, y.n_orders FROM labels l "
        "JOIN b x ON x.user_id = l.user_id AND x.updated_at < l.label_ts "
        "JOIN b y ON y.user_id = l.user_id",
        "L2",
    ),
    (
        "three-level cte reuse",
        "WITH a AS (SELECT * FROM user_stats), b AS (SELECT * FROM a), c AS (SELECT * FROM b) "
        f"{SEL}, x.total_spend, y.n_orders FROM labels l "
        "JOIN c x ON x.user_id = l.user_id AND x.updated_at < l.label_ts "
        "JOIN c y ON y.user_id = l.user_id",
        "L2",
    ),
    (
        "nested cte in two subqueries",
        "WITH e0 AS (SELECT * FROM events), ev AS (SELECT * FROM e0) "
        f"{SEL}, (SELECT SUM(amount) FROM ev WHERE ev.user_id = l.user_id "
        "AND ev.ingested_at < l.label_ts) a, "
        "(SELECT SUM(amount) FROM ev WHERE ev.user_id = l.user_id) b FROM labels l",
        "L2",
    ),
    # a bound that does not filter the reference's rows
    (
        "FILTER clause bounds one aggregate only",
        f"{SEL}, COUNT(*) n, SUM(e.amount) FILTER (WHERE e.ingested_at < l.label_ts) a "
        "FROM labels l JOIN events e ON e.user_id = l.user_id GROUP BY 1, 2, 3",
        "L3",
    ),
    (
        "scalar subquery mentions outer column",
        f"{SEL}, e.amount, (SELECT COUNT(*) FROM entities x WHERE x.user_id = e.user_id "
        "AND e.ingested_at < l.label_ts) c FROM labels l JOIN events e ON e.user_id = l.user_id",
        "L2",
    ),
    (
        "NOT EXISTS inverts the condition",
        f"{SEL}, e.amount FROM labels l JOIN events e ON e.user_id = l.user_id "
        "WHERE NOT EXISTS (SELECT 1 FROM entities x WHERE x.user_id = e.user_id "
        "AND e.ingested_at < l.label_ts)",
        "L2",
    ),
    (
        "NOT IN subquery mentions outer column",
        f"{SEL}, e.amount FROM labels l JOIN events e ON e.user_id = l.user_id "
        "WHERE e.user_id NOT IN "
        "(SELECT x.user_id FROM entities x WHERE e.ingested_at < l.label_ts)",
        "L2",
    ),
    (
        "bound in ON of a LEFT JOIN to another table",
        f"{SEL}, e.amount FROM labels l JOIN events e ON e.user_id = l.user_id "
        "LEFT JOIN entities x ON x.user_id = l.user_id AND e.ingested_at < l.label_ts",
        "L2",
    ),
    (
        "bound in ON of a RIGHT JOIN filters the left side only",
        f"{SEL}, e.amount FROM labels l RIGHT JOIN events e "
        "ON e.user_id = l.user_id AND e.ingested_at < l.label_ts",
        "L2",
    ),
    (
        "bound in ON of a FULL JOIN filters nothing",
        f"{SEL}, e.amount FROM labels l FULL JOIN events e "
        "ON e.user_id = l.user_id AND e.ingested_at < l.label_ts",
        "L2",
    ),
    # windows
    (
        "DESC running sum",
        windowed("SUM(total_spend) OVER (PARTITION BY user_id ORDER BY updated_at DESC)"),
        "L1",
    ),
    (
        "DESC first_value",
        windowed("FIRST_VALUE(total_spend) OVER (PARTITION BY user_id ORDER BY updated_at DESC)"),
        "L1",
    ),
    (
        "DESC lag",
        windowed("LAG(total_spend) OVER (PARTITION BY user_id ORDER BY updated_at DESC)"),
        "L1",
    ),
    (
        "negative lag",
        windowed("LAG(total_spend, -1) OVER (PARTITION BY user_id ORDER BY updated_at)"),
        "L1",
    ),
    ("cume_dist", windowed("CUME_DIST() OVER (PARTITION BY user_id ORDER BY updated_at)"), "L3"),
    (
        "percent_rank",
        windowed("PERCENT_RANK() OVER (PARTITION BY user_id ORDER BY updated_at)"),
        "L3",
    ),
    ("ntile", windowed("NTILE(4) OVER (PARTITION BY user_id ORDER BY updated_at)"), "L3"),
    (
        "row_number desc latest-row filter",
        "WITH f AS (SELECT *, ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY updated_at DESC) rn "
        f"FROM user_stats) {SEL}, f.total_spend FROM labels l "
        "JOIN f ON f.user_id = l.user_id AND f.rn = 1 AND f.updated_at < l.label_ts",
        "L1",
    ),
    (
        "rank from the end of the spine",
        f"{SEL}, ROW_NUMBER() OVER (PARTITION BY l.user_id ORDER BY l.label_ts DESC) AS k "
        "FROM labels l",
        "L1",
    ),
    (
        "later labels of the same user",
        f"{SEL}, (SELECT COUNT(*) FROM labels l2 WHERE l2.user_id = l.user_id "
        "AND l2.label_ts > l.label_ts) AS n FROM labels l",
        "L1",
    ),
    (
        "max over every label of the user",
        f"{SEL}, MAX(l2.label_ts) AS last_ts FROM labels l "
        "JOIN labels l2 ON l2.user_id = l.user_id GROUP BY 1, 2, 3",
        "L3",
    ),
    (
        "window over another copy of the labels",
        f"{SEL}, (SELECT MAX(k) FROM (SELECT ROW_NUMBER() OVER (PARTITION BY l2.user_id "
        "ORDER BY l2.label_ts DESC) AS k FROM labels l2 WHERE l2.user_id = l.user_id) t) AS n "
        "FROM labels l",
        "L1",
    ),
    # spine equality must be exact
    (
        "equality through month truncation",
        f"{SEL}, s.total_spend FROM labels l JOIN labels l2 ON l2.user_id = l.user_id "
        "AND DATE_TRUNC('month', l2.label_ts) = DATE_TRUNC('month', l.label_ts) "
        "JOIN user_stats s ON s.user_id = l.user_id AND s.updated_at < l2.label_ts",
        "L2",
    ),
    (
        "equality through cast to date",
        f"{SEL}, s.total_spend FROM labels l JOIN labels l2 ON l2.user_id = l.user_id "
        "AND CAST(l2.label_ts AS DATE) = CAST(l.label_ts AS DATE) "
        "JOIN user_stats s ON s.user_id = l.user_id AND s.updated_at < l2.label_ts",
        "L2",
    ),
    # one branch of a union leaks
    (
        "leaky union branch",
        f"{FEAT} UNION ALL {SEL}, s.total_spend FROM labels l "
        "JOIN user_stats s ON s.user_id = l.user_id",
        "L2",
    ),
    (
        "correlated MAX without a bound",
        f"{SEL}, s.total_spend FROM labels l JOIN user_stats s ON s.user_id = l.user_id "
        "AND s.updated_at = (SELECT MAX(s2.updated_at) FROM user_stats s2 "
        "WHERE s2.user_id = l.user_id)",
        "L2",
    ),
    (
        "correlated MAX with inclusive bound",
        f"{SEL}, s.total_spend FROM labels l JOIN user_stats s ON s.user_id = l.user_id "
        "AND s.updated_at = (SELECT MAX(s2.updated_at) FROM user_stats s2 "
        "WHERE s2.user_id = l.user_id AND s2.updated_at <= l.label_ts)",
        "L5",
    ),
    (
        "validity end reaches the output",
        "WITH v AS (SELECT user_id, updated_at, total_spend, LEAD(updated_at) OVER "
        "(PARTITION BY user_id ORDER BY updated_at) AS valid_to FROM user_stats) "
        f"{SEL}, v.valid_to AS next_update FROM labels l JOIN v ON v.user_id = l.user_id "
        "AND v.updated_at < l.label_ts",
        "L1",
    ),
    (
        "validity interval without a bound on the version time",
        "WITH v AS (SELECT user_id, updated_at, total_spend, LEAD(updated_at) OVER "
        f"(PARTITION BY user_id ORDER BY updated_at) AS valid_to FROM user_stats) {SEL}, "
        "v.total_spend FROM labels l JOIN v ON v.user_id = l.user_id "
        "AND (v.valid_to IS NULL OR v.valid_to >= l.label_ts)",
        "L1",
    ),
    (
        "validity end used as a feature value",
        "WITH v AS (SELECT user_id, updated_at, total_spend, LEAD(updated_at) OVER "
        "(PARTITION BY user_id ORDER BY updated_at) AS valid_to FROM user_stats) "
        f"{SEL}, CASE WHEN v.valid_to IS NULL THEN 1 ELSE 0 END AS is_last "
        "FROM labels l JOIN v ON v.user_id = l.user_id AND v.updated_at < l.label_ts",
        "L1",
    ),
    (
        "lead of a value column",
        windowed("LEAD(total_spend) OVER (PARTITION BY user_id ORDER BY updated_at)"),
        "L1",
    ),
    (
        "label used by a cast feature",
        f"{SEL}, CAST(l.churned AS DOUBLE) + 1 AS f FROM labels l",
        "L6",
    ),
    (
        "label filter in a subquery of another table",
        f"{SEL}, (SELECT COUNT(*) FROM user_outcomes o WHERE o.user_id = l.user_id "
        "AND l.churned = 1) AS f FROM labels l",
        "L6",
    ),
]


@pytest.mark.parametrize(
    "sql, rule", [c[1:] for c in LEAKY_SHAPES], ids=[c[0] for c in LEAKY_SHAPES]
)
def test_leaky_shape_is_flagged(sql, rule):
    result, rules = verdict(sql)
    assert result == "leaky" and rule in rules, (result, rules)


CLEAN_SHAPES = {
    "aliased cte with asof join": f"WITH stats AS (SELECT * FROM user_stats) {SEL}, s.total_spend "
    "FROM labels l ASOF JOIN stats s ON s.user_id = l.user_id AND l.label_ts > s.updated_at",
    "aliased cte feeding an aggregate": "WITH ev AS "
    "(SELECT user_id, ingested_at, amount FROM events) "
    f"{SEL}, SUM(x.amount) AS a FROM labels l JOIN ev x ON x.user_id = l.user_id "
    "AND x.ingested_at < l.label_ts GROUP BY 1, 2, 3",
    "nested cte, both uses bounded": "WITH a AS (SELECT * FROM user_stats), b AS (SELECT * FROM a) "
    f"{SEL}, x.total_spend, y.n_orders FROM labels l "
    "ASOF JOIN b x ON x.user_id = l.user_id AND l.label_ts > x.updated_at "
    "ASOF JOIN b y ON y.user_id = l.user_id AND l.label_ts > y.updated_at",
    "bound written inside a nested feature cte": "WITH f AS (SELECT l.user_id, l.label_ts, "
    "s.total_spend FROM labels l JOIN user_stats s ON s.user_id = l.user_id "
    "AND s.updated_at < l.label_ts), g AS (SELECT * FROM f) "
    f"{SEL}, a.total_spend, b.total_spend AS again FROM labels l "
    "JOIN g a ON a.user_id = l.user_id AND a.label_ts = l.label_ts "
    "JOIN g b ON b.user_id = l.user_id AND b.label_ts = l.label_ts",
    "lateral subquery with bound": f"{SEL}, t.a FROM labels l, LATERAL (SELECT SUM(e.amount) a "
    "FROM events e WHERE e.user_id = l.user_id AND e.ingested_at < l.label_ts) t",
    "using join on a bounded feature cte": "WITH a AS (SELECT l.user_id, l.label_ts, COUNT(*) n "
    "FROM labels l JOIN events e ON e.user_id = l.user_id AND e.ingested_at < l.label_ts "
    f"GROUP BY 1, 2) {SEL}, a.n FROM labels l LEFT JOIN a USING (user_id, label_ts)",
    "two using joins": "WITH a AS (SELECT l.user_id, l.label_ts, COUNT(*) n FROM labels l "
    "JOIN events e ON e.user_id = l.user_id AND e.ingested_at < l.label_ts GROUP BY 1, 2), "
    "b AS (SELECT l.user_id, l.label_ts, MAX(s.total_spend) t FROM labels l "
    "JOIN user_stats s ON s.user_id = l.user_id AND s.updated_at < l.label_ts GROUP BY 1, 2) "
    f"{SEL}, a.n, b.t FROM labels l LEFT JOIN a USING (user_id, label_ts) "
    "LEFT JOIN b USING (user_id, label_ts)",
    "scd2 validity interval": "WITH v AS (SELECT user_id, updated_at, total_spend, "
    "LEAD(updated_at) OVER (PARTITION BY user_id ORDER BY updated_at) AS valid_to "
    f"FROM user_stats) {SEL}, v.total_spend FROM labels l JOIN v ON v.user_id = l.user_id "
    "AND v.updated_at < l.label_ts AND (v.valid_to IS NULL OR v.valid_to >= l.label_ts)",
    "label cast to integer": "SELECT l.user_id, l.label_ts, CAST(l.churned AS INTEGER) AS churned, "
    f"s.total_spend FROM labels l {STATS}",
    "label double colon cast": "SELECT l.user_id, l.label_ts, l.churned::INT AS churned, "
    f"s.total_spend FROM labels l {STATS}",
    "label coalesce with literal": "SELECT l.user_id, l.label_ts, "
    f"COALESCE(l.churned, 0) AS churned, s.total_spend FROM labels l {STATS}",
    "label filter inside a spine cte": "WITH sp AS (SELECT user_id, label_ts, churned FROM labels "
    "WHERE churned IS NOT NULL) SELECT p.user_id, p.label_ts, p.churned, s.total_spend "
    "FROM sp p ASOF JOIN user_stats s ON s.user_id = p.user_id AND p.label_ts > s.updated_at",
    "label filter inside a derived spine": "SELECT p.user_id, p.label_ts, p.churned, s.total_spend "
    "FROM (SELECT * FROM labels WHERE churned IN (0, 1)) p "
    "ASOF JOIN user_stats s ON s.user_id = p.user_id AND p.label_ts > s.updated_at",
    "label renamed in the spine and back": "WITH sp AS (SELECT user_id, label_ts, churned AS y "
    "FROM labels) SELECT p.user_id, p.label_ts, p.y AS churned, s.total_spend "
    "FROM sp p ASOF JOIN user_stats s ON s.user_id = p.user_id AND p.label_ts > s.updated_at",
    "label having filter": f"{SEL}, MAX(s.total_spend) AS m FROM labels l JOIN user_stats s "
    "ON s.user_id = l.user_id AND s.updated_at < l.label_ts GROUP BY 1, 2, 3 "
    "HAVING l.churned IS NOT NULL",
    "strict ingestion, inclusive event-time window": f"{AGG} AND e.ingested_at < l.label_ts "
    "AND e.event_ts BETWEEN l.label_ts - INTERVAL 30 DAY AND l.label_ts GROUP BY 1, 2, 3",
    "strict ingestion, inclusive event-time bound": f"{AGG} AND e.ingested_at < l.label_ts "
    "AND e.event_ts <= l.label_ts GROUP BY 1, 2, 3",
    "correlated MAX picks the latest earlier row": f"{SEL}, s.total_spend FROM labels l "
    "JOIN user_stats s ON s.user_id = l.user_id AND s.updated_at = (SELECT MAX(s2.updated_at) "
    "FROM user_stats s2 WHERE s2.user_id = l.user_id AND s2.updated_at < l.label_ts)",
    "union of two correct branches": f"{FEAT} UNION ALL {FEAT}",
    "prediction_ts cast in the output": "SELECT l.user_id, CAST(l.label_ts AS TIMESTAMP) AS "
    f"label_ts, l.churned, s.total_spend FROM labels l {STATS}",
    "qualify latest row under a strict bound": f"{SEL}, s.total_spend FROM labels l "
    "JOIN user_stats s ON s.user_id = l.user_id AND s.updated_at < l.label_ts "
    "QUALIFY ROW_NUMBER() OVER (PARTITION BY l.user_id, l.label_ts ORDER BY s.updated_at DESC) = 1",
    "desc window over rows bounded in its own scope": "WITH f AS (SELECT s.user_id, s.updated_at, "
    "l.label_ts, ROW_NUMBER() OVER (PARTITION BY s.user_id, l.label_ts ORDER BY s.updated_at DESC) "
    "rn, s.total_spend FROM labels l JOIN user_stats s ON s.user_id = l.user_id "
    f"AND s.updated_at < l.label_ts) {SEL}, f.total_spend FROM labels l "
    "JOIN f ON f.user_id = l.user_id AND f.label_ts = l.label_ts AND f.rn = 1",
    "trailing window excluding the current row": windowed(
        "SUM(total_spend) OVER (PARTITION BY user_id ORDER BY updated_at "
        "ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING)"
    ),
    "trailing lag and ascending rank": windowed(
        "total_spend - LAG(total_spend) OVER (PARTITION BY user_id ORDER BY updated_at)"
    ),
    "window to the end over bounded rows": f"{SEL}, SUM(s.total_spend) OVER (PARTITION BY "
    "l.user_id, l.label_ts ORDER BY s.updated_at ROWS BETWEEN UNBOUNDED PRECEDING AND "
    "UNBOUNDED FOLLOWING) AS f FROM labels l JOIN user_stats s ON s.user_id = l.user_id "
    "AND s.updated_at < l.label_ts",
    "ascending rank among earlier labels": f"{SEL}, ROW_NUMBER() OVER (PARTITION BY l.user_id "
    "ORDER BY l.label_ts) AS k FROM labels l",
    "count of earlier labels": f"{SEL}, (SELECT COUNT(*) FROM labels l2 WHERE l2.user_id = "
    "l.user_id AND l2.label_ts < l.label_ts) AS n FROM labels l",
    "second label copy equated exactly": f"{SEL}, s.total_spend FROM labels l JOIN labels l2 "
    "ON l2.user_id = l.user_id AND l2.label_ts = l.label_ts JOIN user_stats s "
    "ON s.user_id = l.user_id AND s.updated_at < l2.label_ts",
    "exists with correlated bound": f"{SEL}, EXISTS (SELECT 1 FROM events e WHERE e.user_id = "
    "l.user_id AND e.ingested_at < l.label_ts) AS seen FROM labels l",
    "left join with the bound in on": f"{SEL}, e.amount FROM labels l LEFT JOIN events e "
    "ON e.user_id = l.user_id AND e.ingested_at < l.label_ts AND e.event_ts < l.label_ts",
    "right join keeping the bounded side": f"{SEL}, e.amount FROM events e RIGHT JOIN labels l "
    "ON e.user_id = l.user_id AND e.ingested_at < l.label_ts AND e.event_ts < l.label_ts",
    "semi join with the bound in on": f"{SEL}, l.user_id AS seen_by FROM labels l "
    "SEMI JOIN events e ON e.user_id = l.user_id AND e.ingested_at < l.label_ts",
    "rolling average over bounded rows": f"{SEL}, AVG(e.amount) OVER (PARTITION BY l.user_id, "
    "l.label_ts ORDER BY e.event_ts ROWS BETWEEN 6 PRECEDING AND CURRENT ROW) AS r "
    "FROM labels l JOIN events e ON e.user_id = l.user_id "
    "AND e.ingested_at < l.label_ts AND e.event_ts < l.label_ts",
    "partition count per prediction": f"{SEL}, COUNT(*) OVER (PARTITION BY l.user_id, l.label_ts) "
    "AS n FROM labels l JOIN events e ON e.user_id = l.user_id "
    "AND e.ingested_at < l.label_ts AND e.event_ts < l.label_ts",
    "anti join on earlier events": f"{SEL} FROM labels l WHERE NOT EXISTS (SELECT 1 FROM events e "
    "WHERE e.user_id = l.user_id AND e.ingested_at < l.label_ts AND e.event_ts < l.label_ts)",
    "spine and feature ctes with a date filter": "WITH sp AS (SELECT user_id, label_ts, churned "
    "FROM labels WHERE label_ts >= TIMESTAMP '2026-01-15'), ft AS (SELECT sp.user_id, "
    "sp.label_ts, COUNT(*) n FROM sp JOIN events e ON e.user_id = sp.user_id AND "
    "e.ingested_at < sp.label_ts GROUP BY 1, 2) SELECT sp.user_id, sp.label_ts, sp.churned, ft.n "
    "FROM sp LEFT JOIN ft ON ft.user_id = sp.user_id AND ft.label_ts = sp.label_ts",
    "distinct spine with ordering": f"SELECT DISTINCT l.user_id, l.label_ts, l.churned, "
    f"s.total_spend FROM labels l {STATS} ORDER BY l.label_ts LIMIT 1000",
    "timezone-aware table with an explicit cast": f"{SEL}, COUNT(c.page) AS n FROM labels l "
    "LEFT JOIN clicks c ON c.user_id = l.user_id AND CAST(c.click_ts AS TIMESTAMP) < l.label_ts "
    "GROUP BY 1, 2, 3",
    "static table beside a bounded feature": f"{SEL}, u.country, s.total_spend FROM labels l "
    f"JOIN entities u ON u.user_id = l.user_id {STATS}",
}


@pytest.mark.parametrize("sql", CLEAN_SHAPES.values(), ids=CLEAN_SHAPES.keys())
def test_correct_shape_stays_clean(sql):
    assert verdict(sql) == ("clean", set())


def test_clean_side_is_at_least_as_large_as_the_leaky_side():
    assert len(CLEAN_SHAPES) >= 15


# --- replay evidence and sampling ----------------------------------------------------------

BENCH_SPEC = LeakageSpec.from_yaml("bench/leakage/spec.yaml")
EVENTS_14D = """
SELECT l.user_id, l.label_ts, l.churned, SUM(e.amount) AS amt
FROM labels l LEFT JOIN events e ON e.user_id = l.user_id
  AND e.{column} < l.label_ts AND e.event_ts >= l.label_ts - INTERVAL 14 DAY
{where}
GROUP BY l.user_id, l.label_ts, l.churned
"""


@pytest.fixture(scope="module")
def bench_world(tmp_path_factory):
    catalog = LakehouseCatalog.local(tmp_path_factory.mktemp("bench-world"))
    build_world(catalog, WorldConfig(seed=42))
    return catalog, Replayer(catalog, BENCH_SPEC, seed=42)


def events_query(column: str, where: str = "") -> str:
    return EVENTS_14D.format(column=column, where=where)


@pytest.mark.parametrize(
    "where",
    [
        "WHERE l.label_ts BETWEEN TIMESTAMP '2026-01-27 10:00' AND TIMESTAMP '2026-01-28 23:00'",
        "WHERE l.label_ts > TIMESTAMP '2026-01-31 00:00'",
        "WHERE l.user_id < 8",
    ],
)
def test_late_data_is_found_when_the_query_keeps_a_recent_slice(bench_world, where):
    """Prediction times are sampled from the query output, so a filtered training set is
    still replayed (it used to compare nothing and pass)."""
    catalog, replayer = bench_world
    sql = events_query("event_ts", where)
    assert replayer.run(sql).status is ReplayStatus.MISMATCH
    assert validate(sql, BENCH_SPEC, catalog=catalog, replay=replayer).verdict == "leaky"


@pytest.mark.parametrize(
    "where, reason, hinted",
    [
        ("WHERE l.label_ts >= TIMESTAMP '2026-03-01'", "no label prediction time", True),
        ("WHERE l.user_id = 3", "needs at least", True),
        # the event-time variant leaks visibly in these 14 rows, so replay finds it anyway
        ("WHERE l.label_ts > TIMESTAMP '2026-01-31 00:00'", "needs at least", False),
    ],
)
def test_replay_without_enough_evidence_never_passes(bench_world, where, reason, hinted):
    """Too little compared is never a PASS. It blocks a query whose static hint only replay can
    settle (event-time bound), and is only recorded for one that static analysis cleared."""
    catalog, replayer = bench_world
    if hinted:
        sql = events_query("event_ts", where)
        result = replayer.run(sql)
        assert result.status is ReplayStatus.UNVERIFIABLE and reason in result.reason
        assert validate(sql, BENCH_SPEC, catalog=catalog, replay=replayer).verdict == "unverifiable"

    cleared = events_query("ingested_at", where)
    report = validate(cleared, BENCH_SPEC, catalog=catalog, replay=replayer)
    assert report.replay.status is ReplayStatus.UNVERIFIABLE and reason in report.replay.reason
    assert report.replay.insufficient_evidence
    assert report.verdict == "clean" and not report.findings


def test_replay_that_cannot_run_still_blocks_a_clean_query(bench_world):
    catalog, replayer = bench_world
    sql = "SELECT l.user_id, l.label_ts, l.churned, l.nope FROM labels l"
    report = validate(sql, BENCH_SPEC, catalog=catalog, replay=replayer)
    assert report.verdict == "unverifiable"


def test_static_only_event_time_leak_is_not_cleared_by_an_empty_replay(bench_world):
    catalog, replayer = bench_world
    sql = events_query("event_ts", "WHERE l.label_ts >= TIMESTAMP '2026-03-01'")
    report = validate(sql, BENCH_SPEC, catalog=catalog, replay=replayer)
    assert report.leak_types == ["L4"] and report.verdict == "unverifiable"


def test_enough_rows_and_times_still_pass(bench_world):
    _, replayer = bench_world
    result = replayer.run(events_query("ingested_at"))
    assert result.status is ReplayStatus.PASS
    assert result.rows_compared >= 20 and result.timestamps_compared >= 3


def test_sampling_with_unique_timestamps_covers_the_whole_range():
    stamps = Counter({datetime(2026, 1, 1) + timedelta(hours=7 * i): 1 for i in range(1000)})
    points = sample_points(stamps, 24, seed=0)
    ordered = sorted(stamps)
    assert len(points) == 24 and set(points) <= set(stamps)
    assert points != ordered[:24] and points[:12] != ordered[:12]
    assert ordered.index(points[0]) < 1000 // 24 and ordered.index(points[-1]) >= 1000 - 1000 // 24
    gaps = [ordered.index(b) - ordered.index(a) for a, b in zip(points, points[1:], strict=False)]
    assert max(gaps) < 3 * 1000 / 24
    assert (
        sample_points(stamps, 24, seed=0) == points and sample_points(stamps, 24, seed=1) != points
    )


def test_sampling_prefers_shared_timestamps_then_spreads_the_rest():
    shared = {datetime(2026, 2, d): 40 for d in range(1, 7)}
    stamps = Counter(
        {**shared, **{datetime(2026, 1, 1) + timedelta(hours=i): 1 for i in range(500)}}
    )
    points = sample_points(stamps, 12, seed=3)
    assert set(shared) <= set(points) and len(points) == 12


# --- timezone-aware snapshot properties ---------------------------------------------------


@pytest.mark.parametrize(
    "logical",
    ["{day}T00:00:00+00:00", "{day}T07:00:00+07:00", "{day}T00:00:00Z"],
    ids=["utc", "offset", "zulu"],
)
def test_aware_logical_snapshot_time_does_not_crash(tmp_path, logical):
    catalog = LakehouseCatalog.local(tmp_path / "wh")
    users = list(range(6))
    stamps = [datetime(2026, 1, d) for d in (3, 5, 8, 10) for _ in users]
    catalog.create_table(
        "labels",
        pa.table(
            {
                "user_id": pa.array(users * 4, pa.int64()),
                "label_ts": pa.array(stamps, pa.timestamp("us")),
                "churned": pa.array([0] * len(stamps), pa.int64()),
            }
        ),
    )
    schema = pa.schema(
        [("user_id", pa.int64()), ("updated_at", pa.timestamp("us")), ("v", pa.float64())]
    )
    stats = catalog.create_empty("stats", schema)
    for day in (1, 7):
        rows = pa.table(
            {"user_id": users, "updated_at": [datetime(2026, 1, day)] * 6, "v": [float(day)] * 6},
            schema=schema,
        )
        stats.append(
            rows,
            snapshot_properties={LOGICAL_TS_PROPERTY: logical.format(day=f"2026-01-{day:02d}")},
        )
    visible = [s.visible_at for s in catalog.snapshots("stats")]
    assert visible == [datetime(2026, 1, 1), datetime(2026, 1, 7)]  # naive UTC
    spec = LeakageSpec.from_dict(
        {
            "label_table": "labels",
            "entity_keys": ["user_id"],
            "prediction_ts": "label_ts",
            "label_columns": ["churned"],
            "time_columns": {"stats": "updated_at"},
        }
    )
    sql = (
        "SELECT l.user_id, l.label_ts, l.churned, s.v FROM labels l "
        "ASOF JOIN stats s ON s.user_id = l.user_id AND l.label_ts > s.updated_at"
    )
    assert Replayer(catalog, spec).run(sql).status is ReplayStatus.PASS
    assert validate(sql, spec, catalog=catalog, replay=True).verdict == "clean"
    assert datetime(2026, 1, 1, tzinfo=UTC)  # aware input to snapshot_as_of is accepted
    assert catalog.snapshot_as_of("stats", datetime(2026, 1, 3, tzinfo=UTC)) is not None


# --- spine per path of use, window partitions, wrappers, USING, SCD2, label filters ---------

SPINE = "WITH spine AS (SELECT user_id, label_ts, churned FROM labels)"
SPINE_EV = (
    f"{SPINE}, ev AS (SELECT s.user_id, s.label_ts, COUNT(*) n FROM spine s "
    "JOIN events e ON e.user_id = s.user_id AND e.ingested_at < s.label_ts GROUP BY 1, 2)"
)
VALID = (
    "WITH v AS (SELECT user_id, updated_at, total_spend, LEAD(updated_at) OVER "
    "(PARTITION BY user_id ORDER BY updated_at) AS valid_to FROM user_stats) "
    f"{SEL}, v.total_spend FROM labels l JOIN v ON v.user_id = l.user_id "
    "AND v.updated_at < l.label_ts AND "
)
BOUNDED = "FROM labels l JOIN events e ON e.user_id = l.user_id AND e.ingested_at < l.label_ts"

PATH_LEAKS = {
    "spine cte joined back on the entity only": (
        f"{SPINE_EV} SELECT sp.user_id, sp.label_ts, sp.churned, ev.n FROM spine sp "
        "LEFT JOIN ev ON ev.user_id = sp.user_id",
        "L2",
    ),
    "spine cte joined back with USING on the entity only": (
        f"{SPINE_EV} SELECT sp.user_id, sp.label_ts, sp.churned, ev.n FROM spine sp "
        "LEFT JOIN ev USING (user_id)",
        "L2",
    ),
    "spine cte joined back on later predictions": (
        f"{SPINE_EV} SELECT sp.user_id, sp.label_ts, sp.churned, ev.n FROM spine sp "
        "LEFT JOIN ev ON ev.user_id = sp.user_id AND ev.label_ts >= sp.label_ts",
        "L1",
    ),
    "spine cte feature grouped by the entity only": (
        f"{SPINE}, f AS (SELECT s.user_id, SUM(e.amount) amt FROM spine s JOIN events e "
        "ON e.user_id = s.user_id AND e.ingested_at < s.label_ts GROUP BY s.user_id) "
        "SELECT sp.user_id, sp.label_ts, sp.churned, f.amt FROM spine sp "
        "JOIN f ON f.user_id = sp.user_id",
        "L3",
    ),
    "future label count through the spine cte": (
        f"{SPINE} SELECT a.user_id, a.label_ts, a.churned, (SELECT COUNT(*) FROM spine b "
        "WHERE b.user_id = a.user_id AND b.label_ts > a.label_ts) AS n_future FROM spine a",
        "L1",
    ),
    "feature cte used twice, one use not aligned": (
        "WITH f AS (SELECT l.user_id, l.label_ts, COUNT(*) n "
        f"{BOUNDED} GROUP BY 1, 2) {SEL}, a.n, MAX(b.n) AS mb FROM labels l "
        "LEFT JOIN f a ON a.user_id = l.user_id AND a.label_ts = l.label_ts "
        "LEFT JOIN f b ON b.user_id = l.user_id GROUP BY 1, 2, 3, 4",
        "L3",
    ),
    "window partitioned by the entity after a bounded join": (
        f"SELECT DISTINCT l.user_id, l.label_ts, l.churned, SUM(e.amount) OVER "
        f"(PARTITION BY l.user_id) AS tot {BOUNDED}",
        "L3",
    ),
    "window partitioned by the entity inside a feature cte": (
        "WITH x AS (SELECT l.user_id, l.label_ts, AVG(s.total_spend) OVER "
        "(PARTITION BY l.user_id) a FROM labels l JOIN user_stats s ON s.user_id = l.user_id "
        f"AND s.updated_at < l.label_ts) {SEL}, MAX(x.a) AS a FROM labels l JOIN x "
        "ON x.user_id = l.user_id AND x.label_ts = l.label_ts GROUP BY 1, 2, 3",
        "L3",
    ),
    "latest event by id": (
        "WITH last AS (SELECT * FROM events QUALIFY ROW_NUMBER() OVER "
        f"(PARTITION BY user_id ORDER BY event_id DESC) = 1) {SEL}, last.amount "
        "FROM labels l LEFT JOIN last ON last.user_id = l.user_id "
        "AND last.ingested_at < l.label_ts",
        "L1",
    ),
    "rank by value before the bound": (
        "WITH best AS (SELECT * FROM user_stats QUALIFY RANK() OVER "
        f"(PARTITION BY user_id ORDER BY total_spend DESC) = 1) {SEL}, b.total_spend "
        "FROM labels l LEFT JOIN best b ON b.user_id = l.user_id AND b.updated_at < l.label_ts",
        "L1",
    ),
    "running max ordered by a value": (
        windowed("MAX(n_orders) OVER (PARTITION BY user_id ORDER BY total_spend)"),
        "L1",
    ),
    "ordered by a value over bounded rows that mix predictions": (
        f"{SEL}, MAX(s.n_orders) OVER (PARTITION BY l.user_id ORDER BY s.total_spend) AS m "
        "FROM labels l JOIN user_stats s ON s.user_id = l.user_id AND s.updated_at < l.label_ts",
        "L1",
    ),
    "predicate mixing the label with a feature choosing rows": (
        f"{SEL}, e.amount {BOUNDED} WHERE (l.churned = 1 AND e.amount > 100) "
        "OR (l.churned = 0 AND e.amount <= 100)",
        "L6",
    ),
    "predicate comparing a feature with the label": (
        f"{FEAT} WHERE s.n_orders > l.churned",
        "L6",
    ),
    "label filter in a feature cte": (
        f"WITH k AS (SELECT user_id, COUNT(*) n FROM labels WHERE churned = 1 GROUP BY 1) "
        f"{SEL}, k.n FROM labels l LEFT JOIN k ON k.user_id = l.user_id",
        "L6",
    ),
    "validity end compared inside a value expression": (
        VALID + "(v.valid_to IS NULL OR CAST(v.valid_to AS DATE) >= l.label_ts)",
        "L1",
    ),
}


@pytest.mark.parametrize("sql, rule", PATH_LEAKS.values(), ids=PATH_LEAKS.keys())
def test_path_and_window_leaks_are_flagged(sql, rule):
    result, rules = verdict(sql)
    assert result == "leaky" and rule in rules, (result, rules)


PATH_CLEAN = {
    "spine cte joined back on key and prediction time": f"{SPINE_EV} SELECT sp.user_id, "
    "sp.label_ts, sp.churned, ev.n FROM spine sp LEFT JOIN ev "
    "ON ev.user_id = sp.user_id AND ev.label_ts = sp.label_ts",
    "spine cte joined back with USING on key and time": f"{SPINE_EV} SELECT user_id, label_ts, "
    "churned, ev.n FROM spine sp LEFT JOIN ev USING (user_id, label_ts)",
    "spine cte reused with both uses aligned": f"{SPINE_EV} SELECT sp.user_id, sp.label_ts, "
    "sp.churned, a.n, b.n AS again FROM spine sp "
    "LEFT JOIN ev a ON a.user_id = sp.user_id AND a.label_ts = sp.label_ts "
    "LEFT JOIN ev b ON b.user_id = sp.user_id AND b.label_ts = sp.label_ts",
    "earlier-label count through the spine cte": f"{SPINE} SELECT a.user_id, a.label_ts, "
    "a.churned, (SELECT COUNT(*) FROM spine b WHERE b.user_id = a.user_id "
    "AND b.label_ts < a.label_ts) AS n_prior FROM spine a",
    "window partitioned by the prediction": f"SELECT DISTINCT l.user_id, l.label_ts, l.churned, "
    f"SUM(e.amount) OVER (PARTITION BY l.user_id, l.label_ts) AS tot {BOUNDED}",
    "window per outer row in a lateral": f"{SEL}, t.latest FROM labels l LEFT JOIN LATERAL "
    "(SELECT RANK() OVER (ORDER BY s.total_spend DESC) AS rk, s.total_spend AS latest "
    "FROM user_stats s WHERE s.user_id = l.user_id AND s.updated_at < l.label_ts "
    "QUALIFY rk = 1) t ON TRUE",
    "value-ordered rank over rows bounded for the prediction": f"{SEL}, s.total_spend "
    "FROM labels l JOIN user_stats s ON s.user_id = l.user_id AND s.updated_at < l.label_ts "
    "QUALIFY RANK() OVER (PARTITION BY l.user_id, l.label_ts ORDER BY s.total_spend DESC) = 1",
    "ascending running window over the table's own time": windowed(
        "SUM(total_spend) OVER (PARTITION BY user_id ORDER BY updated_at)"
    ),
    "ascending window over a truncated time": "WITH daily AS (SELECT user_id, "
    "CAST(updated_at AS DATE) AS d, MAX(total_spend) AS sp FROM user_stats GROUP BY 1, 2), "
    "roll AS (SELECT user_id, d, AVG(sp) OVER (PARTITION BY user_id ORDER BY d "
    "ROWS BETWEEN 6 PRECEDING AND CURRENT ROW) AS sp7 FROM daily) "
    f"{SEL}, r.sp7 FROM labels l ASOF JOIN roll r ON r.user_id = l.user_id "
    "AND r.d < CAST(l.label_ts AS DATE)",
    "final cte wrapper with grouping": f"WITH final AS ({SEL}, SUM(e.amount) amt {BOUNDED} "
    "GROUP BY 1, 2, 3) SELECT * FROM final",
    "final cte wrapper with an explicit label group": f"WITH final AS ({SEL}, SUM(e.amount) "
    f"amt {BOUNDED} GROUP BY l.user_id, l.label_ts, l.churned) SELECT * FROM final",
    "final cte wrapper filtering the label": f"WITH base AS ({SEL}, s.total_spend FROM labels l "
    f"{STATS} WHERE l.churned IN (0, 1)) SELECT b.* FROM base b",
    "final cte wrapper ordering by the label": f"WITH final AS ({FEAT}) SELECT * FROM final "
    "ORDER BY churned",
    "final derived table with a label filter": f"SELECT f.* FROM ({SEL}, s.total_spend FROM "
    f"labels l {STATS} WHERE l.churned IS NOT NULL) f",
    "label filter beside a feature filter": f"{FEAT} WHERE l.churned IS NOT NULL "
    "AND l.label_ts >= TIMESTAMP '2026-01-01' AND s.n_orders > 0",
    "unqualified columns after USING": "WITH f AS (SELECT l.user_id, l.label_ts, COUNT(*) n "
    f"{BOUNDED} GROUP BY 1, 2) SELECT user_id, label_ts, churned, f.n FROM labels "
    "LEFT JOIN f USING (user_id, label_ts)",
    "unqualified columns after an inner USING": "WITH f AS (SELECT l.user_id, l.label_ts, "
    f"COUNT(*) n {BOUNDED} GROUP BY 1, 2) SELECT user_id, label_ts, churned, f.n FROM labels "
    "JOIN f USING (user_id, label_ts)",
    "unqualified columns after USING on a spine cte": f"{SPINE_EV} SELECT user_id, label_ts, "
    "churned, ev.n FROM spine LEFT JOIN ev USING (user_id, label_ts)",
    "unqualified columns after a natural join": f"{SPINE_EV} SELECT user_id, label_ts, churned, "
    "ev.n FROM spine NATURAL LEFT JOIN ev",
    "validity end defaulted with a sentinel": VALID
    + "l.label_ts <= COALESCE(v.valid_to, TIMESTAMP '9999-12-31')",
    "validity end defaulted, strict": VALID
    + "l.label_ts < COALESCE(v.valid_to, TIMESTAMP '9999-12-31')",
    "validity end defaulted, reversed": VALID
    + "COALESCE(v.valid_to, TIMESTAMP '9999-12-31') > l.label_ts",
    "prior-prediction gap": f"{SEL}, date_diff('hour', LAG(l.label_ts) OVER (PARTITION BY "
    "l.user_id ORDER BY l.label_ts), l.label_ts) AS gap FROM labels l",
    "first prediction of the entity": f"{FEAT} QUALIFY ROW_NUMBER() OVER (PARTITION BY "
    "l.user_id ORDER BY l.label_ts) = 1",
}


@pytest.mark.parametrize("sql", PATH_CLEAN.values(), ids=PATH_CLEAN.keys())
def test_path_wrapper_and_using_shapes_stay_clean(sql):
    assert verdict(sql) == ("clean", set())


def test_cutoff_subqueries_over_the_label_table_are_a_documented_limitation():
    """Time splits must use a literal cutoff: both detectors flag one computed from the labels."""
    computed = f"{FEAT} WHERE l.label_ts < (SELECT MAX(label_ts) FROM labels)"
    literal = f"{FEAT} WHERE l.label_ts < TIMESTAMP '2026-01-20'"
    assert verdict(computed)[0] == "leaky"
    assert verdict(literal) == ("clean", set())


@pytest.fixture(scope="module")
def prior_labels_replayer(bench_world):
    return bench_world[1]


@pytest.mark.parametrize(
    "sql",
    [
        f"{SEL}, COUNT(*) OVER (PARTITION BY l.user_id ORDER BY l.label_ts ROWS BETWEEN "
        "UNBOUNDED PRECEDING AND 1 PRECEDING) AS n_prior FROM labels l",
        f"{SEL}, (SELECT COUNT(*) FROM labels k WHERE k.user_id = l.user_id "
        "AND k.label_ts < l.label_ts) AS n_prior FROM labels l",
        f"{SEL}, date_diff('hour', LAG(l.label_ts) OVER (PARTITION BY l.user_id "
        "ORDER BY l.label_ts), l.label_ts) AS gap FROM labels l",
        f"{SEL} FROM labels l QUALIFY ROW_NUMBER() OVER (PARTITION BY l.user_id "
        "ORDER BY l.label_ts) = 1",
    ],
    ids=["window count", "subquery count", "gap to previous", "first per user"],
)
def test_features_over_earlier_prediction_rows_replay_identically(prior_labels_replayer, sql):
    result = prior_labels_replayer.run(sql)
    assert result.status is ReplayStatus.PASS, result


@pytest.mark.parametrize(
    "sql",
    [
        f"{SEL}, LAG(l.churned) OVER (PARTITION BY l.user_id ORDER BY l.label_ts) AS prev "
        "FROM labels l",
        f"{SEL}, (SELECT SUM(k.churned) FROM labels k WHERE k.user_id = l.user_id "
        "AND k.label_ts < l.label_ts) AS prior_churn FROM labels l",
    ],
    ids=["previous label value", "sum of earlier labels"],
)
def test_reading_an_earlier_label_value_still_mismatches_in_replay(prior_labels_replayer, sql):
    assert prior_labels_replayer.run(sql).status is ReplayStatus.MISMATCH
