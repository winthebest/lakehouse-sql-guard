"""Leaky queries that an earlier version of the rules or of the replay verdict called clean, found
by replaying every prediction time as an oracle. Each is leaky; every test asserts the product does
not call it clean."""

import pytest

from bench.leakage.world import WorldConfig, build_world
from sqlguard.core.catalog import LakehouseCatalog
from sqlguard.leakage.replay import Replayer
from sqlguard.leakage.spec import LeakageSpec
from sqlguard.leakage.validator import validate

SPEC = LeakageSpec.from_yaml("bench/leakage/spec.yaml")
SEL = "SELECT l.user_id, l.label_ts, l.churned"

KEYED = """WITH sp AS (SELECT user_id, label_ts, churned, {key} AS sk FROM labels),
pit AS (SELECT sp.sk, s.total_spend FROM sp JOIN user_stats s ON s.user_id = sp.user_id
  AND s.updated_at < sp.label_ts
  QUALIFY row_number() OVER (PARTITION BY sp.sk ORDER BY s.updated_at DESC) = 1)
SELECT sp.user_id, sp.label_ts, sp.churned, pit.total_spend
FROM sp LEFT JOIN pit ON pit.sk = sp.sk"""
TL = """WITH tl AS (
  SELECT user_id, label_ts AS ts, 'spine' AS kind, {s_ord} AS ord, churned,
         CAST(NULL AS DOUBLE) AS v
  FROM labels
  UNION ALL
  SELECT user_id, ingested_at AS ts, 'fact' AS kind, 1 AS ord, CAST(NULL AS BIGINT),
         amount FROM events),
run AS (SELECT user_id, ts, kind, churned, SUM(v) OVER (PARTITION BY user_id ORDER BY ts, ord
  ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS carried{extra} FROM tl)
SELECT user_id, ts AS label_ts, churned, {outs} FROM run WHERE kind = 'spine'"""

STATIC_MISSES = {
    # a "surrogate key" of the day: rows of every user and every later prediction that day
    "key_of_the_day": KEYED.format(key="md5(CAST(CAST(label_ts AS DATE) AS VARCHAR))"),
    "self_join_on_day_key": """WITH sp AS (SELECT user_id, label_ts, churned,
   md5(CAST(CAST(label_ts AS DATE) AS VARCHAR)) AS dk FROM labels)
SELECT a.user_id, a.label_ts, a.churned, MAX(b.label_ts) AS last_pred_today
FROM sp a JOIN sp b ON b.dk = a.dk GROUP BY 1, 2, 3""",
    "user_day_key": KEYED.format(
        key="md5(CAST(user_id AS VARCHAR) || '|' || CAST(CAST(label_ts AS DATE) AS VARCHAR))"
    ),
    # tie-break constants that the cast makes equal: facts at the same instant may sort first
    "tie_cast_rounds_to_fact": TL.format(s_ord="CAST(0.6 AS INT)", extra="", outs="carried"),
    "tie_try_cast": TL.format(s_ord="TRY_CAST(0.5 AS INT)", extra="", outs="carried"),
    # the timeline bound is put on the table, so other windows over the same rows inherit it
    "second_window_whole_partition": TL.format(
        s_ord="0",
        extra=", SUM(v) OVER (PARTITION BY user_id) AS total_all",
        outs="carried, total_all",
    ),
    "window_over_carried_values": TL.format(
        s_ord="0",
        extra="",
        outs="carried, MAX(carried) OVER (PARTITION BY user_id) AS final_carried",
    ),
}


@pytest.mark.parametrize("sql", STATIC_MISSES.values(), ids=STATIC_MISSES.keys())
def test_static_does_not_call_a_leaky_query_clean(sql):
    schema = {
        "labels": {"user_id": "BIGINT", "label_ts": "TIMESTAMP", "churned": "BIGINT"},
        "events": {
            "event_id": "BIGINT",
            "user_id": "BIGINT",
            "event_ts": "TIMESTAMP",
            "ingested_at": "TIMESTAMP",
            "amount": "DOUBLE",
        },
        "user_stats": {
            "user_id": "BIGINT",
            "updated_at": "TIMESTAMP",
            "total_spend": "DOUBLE",
            "n_orders": "BIGINT",
        },
    }
    assert validate(sql, SPEC, schema=schema).verdict != "clean"


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    catalog = LakehouseCatalog.local(tmp_path_factory.mktemp("probe-world"))
    build_world(catalog, WorldConfig(seed=42))
    return catalog


SETTLED_LEAKS = {
    # 2 of 409 output rows differ from point-in-time truth; the sampled replay misses them
    "one_user_reads_three_days_ahead": f"""{SEL}, COALESCE(SUM(e.amount), 0) AS spend
FROM labels l LEFT JOIN events e ON e.user_id = l.user_id
 AND (e.ingested_at < l.label_ts OR (l.user_id = 7 AND e.ingested_at < l.label_ts + INTERVAL 3 DAY))
GROUP BY 1, 2, 3""",
    # an explicit read of rows loaded 40 days after prediction time (none exist in the world yet)
    "explicit_future_read": f"""{SEL}, COALESCE(SUM(e.amount), 0) AS later FROM labels l
LEFT JOIN events e ON e.user_id = l.user_id AND e.ingested_at > l.label_ts + INTERVAL 40 DAY
GROUP BY 1, 2, 3""",
    # every event of the user, past and future, filtered on a value no row has yet
    "unbounded_rare_slice": f"""{SEL}, COUNT(e.event_id) AS n FROM labels l LEFT JOIN events e
 ON e.user_id = l.user_id AND e.amount > 100000 GROUP BY 1, 2, 3""",
}


@pytest.mark.parametrize("seed", [42, 0, 7])
@pytest.mark.parametrize("sql", SETTLED_LEAKS.values(), ids=SETTLED_LEAKS.keys())
def test_a_sampled_replay_pass_does_not_clear_a_static_leak(world, sql, seed):
    report = validate(sql, SPEC, catalog=world, replay=Replayer(world, SPEC, seed=seed))
    assert report.verdict != "clean", [f.rule_id for f in report.findings]


def test_label_table_exposure_counts_only_hidden_rows(world):
    import pyarrow as pa
    import pyarrow.compute as pc

    replayer = Replayer(world, SPEC, seed=42)
    ts = max(replayer._label_stamps.values())
    replayer.pinned(ts, history=True)
    column = replayer.labels.column("label_ts")
    at = replayer.labels.filter(pc.equal(column, pa.scalar(ts, type=column.type)))
    entities = {(u,) for u in at.column("user_id").to_pylist()}
    # nothing later exists for these entities, so the pinned read hides nothing from them
    assert replayer._exposed("labels", ts, entities) == 0
