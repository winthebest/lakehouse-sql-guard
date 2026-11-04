"""suggest_fix with a catalog: a rewrite earns `static_and_replay` only when the repaired query
replays point-in-time, and it reports what the repair does to the row count."""

import pytest

from bench.leakage.world import WorldConfig, build_world
from sqlguard.core.catalog import LakehouseCatalog
from sqlguard.leakage import fixes as fixes_module
from sqlguard.leakage import rewrite
from sqlguard.leakage.fixes import Semantics, Verification, suggest_fix
from sqlguard.leakage.replay import Replayer, ReplayStatus
from sqlguard.leakage.spec import LeakageSpec
from sqlguard.leakage.validator import validate

SPEC = LeakageSpec.from_yaml("bench/leakage/spec.yaml")
SEL = "SELECT l.user_id, l.label_ts, l.churned"
EVENT_TIME_ONLY = (
    f"{SEL}, SUM(e.amount) AS amt FROM labels l LEFT JOIN events e "
    "ON e.user_id = l.user_id AND e.event_ts < l.label_ts GROUP BY 1, 2, 3"
)


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    catalog = LakehouseCatalog.local(tmp_path_factory.mktemp("fixes"))
    build_world(catalog, WorldConfig(seed=42))
    return catalog


@pytest.fixture(scope="module")
def replayer(world):
    return Replayer(world, SPEC, seed=42)


def rewrites(sql, world, replayer, spec=SPEC):
    return [f for f in suggest_fix(sql, spec, catalog=world, replay=replayer) if f.rewrites]


def test_late_arrival_fix_is_replayed_and_earns_both_levels(world, replayer):
    assert replayer.run(EVENT_TIME_ONLY).status is ReplayStatus.MISMATCH
    (fix,) = rewrites(EVENT_TIME_ONLY, world, replayer)
    assert fix.verification is Verification.STATIC_AND_REPLAY
    assert replayer.run(fix.after_sql).status is ReplayStatus.PASS
    assert validate(fix.after_sql, SPEC, catalog=world, replay=replayer).verdict == "clean"


def test_without_replay_the_same_fix_is_static_only(world):
    (fix,) = [
        f for f in suggest_fix(EVENT_TIME_ONLY, SPEC, catalog=world, replay=False) if f.rewrites
    ]
    assert fix.verification is Verification.STATIC_ONLY


def test_replay_that_cannot_decide_leaves_the_fix_static_only(world):
    thin = Replayer(world, SPEC, seed=42, min_rows=10_000)
    (fix,) = rewrites(EVENT_TIME_ONLY, world, thin)
    assert fix.verification is Verification.STATIC_ONLY
    assert any("replay could not confirm" in c for c in fix.caveats)


def test_replay_that_still_finds_a_difference_makes_the_fix_unverified(
    world, replayer, monkeypatch
):
    """A rewrite the static check accepts but replay refutes must not be certified."""

    def cosmetic(ctx, ref, rule):
        table = ctx.orig(ref.node)
        _, join = rewrite._select_of(table)
        join.set(
            "on",
            rewrite.conjoin(join.args["on"], rewrite.exp.condition("1 = 1")),
        )
        return rewrite.Edit(rule, ref.describe(), "does nothing about late data")

    monkeypatch.setattr(rewrite, "add_bounds", cosmetic)
    monkeypatch.setattr(fixes_module, "static_findings", lambda *a, **k: [])
    fix = rewrites(EVENT_TIME_ONLY, world, replayer)[-1]
    assert fix.verification is Verification.UNVERIFIED
    assert any("replay still finds" in c for c in fix.caveats)


def test_a_change_in_the_row_count_is_reported(world, replayer):
    inner = EVENT_TIME_ONLY.replace("LEFT JOIN", "JOIN")
    fixes = rewrites(inner, world, replayer)
    text = " ".join(c for f in fixes for c in f.caveats)
    assert "INNER JOIN became LEFT JOIN" in text
    assert "rows instead of" in text


def test_a_fix_that_keeps_the_rows_has_no_row_count_note(world, replayer):
    (fix,) = rewrites(EVENT_TIME_ONLY, world, replayer)
    assert not any("rows instead of" in c for c in fix.caveats)


def test_an_unsettled_leak_next_to_a_fixed_one_is_not_certified_by_replay(world, replayer):
    sql = (
        f"{SEL}, SUM(e.amount) AS amt, SUM(o.refund_after_churn) AS refunds FROM labels l "
        "LEFT JOIN events e ON e.user_id = l.user_id AND e.event_ts < l.label_ts "
        "LEFT JOIN user_outcomes o ON o.user_id = l.user_id GROUP BY 1, 2, 3"
    )
    fixes = suggest_fix(sql, SPEC, catalog=world, replay=replayer)
    assert {f.rule for f in fixes} >= {"L4", "L6"}
    assert all(f.verification is not Verification.STATIC_AND_REPLAY for f in fixes)


AGG = (
    f"{SEL}, SUM(e.amount) a FROM labels l LEFT JOIN events e ON e.user_id = l.user_id "
    "GROUP BY 1, 2, 3"
)
SHAPES = {
    "where on the joined table": (
        f"{SEL}, SUM(e.amount) a FROM labels l LEFT JOIN events e ON e.user_id = l.user_id "
        "WHERE e.amount > 0 GROUP BY 1, 2, 3"
    ),
    "having on a count": (
        f"{SEL}, SUM(e.amount) a FROM labels l LEFT JOIN events e ON e.user_id = l.user_id "
        "GROUP BY 1, 2, 3 HAVING COUNT(e.event_id) > 3"
    ),
    "in subquery": (
        f"{SEL} FROM labels l WHERE l.user_id IN (SELECT e.user_id FROM events e "
        "WHERE e.amount > 100)"
    ),
    "exists subquery": (
        f"{SEL} FROM labels l WHERE EXISTS (SELECT 1 FROM events e WHERE e.user_id = l.user_id)"
    ),
    "derived table filtered on the aggregate": (f"SELECT * FROM ({AGG}) t WHERE a > 0"),
    "cte filtered on the aggregate": f"WITH t AS ({AGG}) SELECT * FROM t WHERE a IS NOT NULL",
    "two levels, renamed, filtered": (
        f"SELECT * FROM (SELECT user_id, label_ts, churned, a AS b FROM ({AGG}) t1) t2 WHERE b > 0"
    ),
    "order by the aggregate with a limit": (
        f"{SEL}, SUM(e.amount) a FROM labels l LEFT JOIN events e ON e.user_id = l.user_id "
        "GROUP BY 1, 2, 3 ORDER BY MAX(e.amount) DESC LIMIT 50"
    ),
    "alias in HAVING": (
        f"{SEL}, SUM(e.amount) a FROM labels l LEFT JOIN events e ON e.user_id = l.user_id "
        "GROUP BY 1, 2, 3 HAVING a > 0"
    ),
    "alias in ORDER BY with LIMIT": (
        f"{SEL}, SUM(e.amount) a FROM labels l LEFT JOIN events e ON e.user_id = l.user_id "
        "GROUP BY 1, 2, 3 ORDER BY a DESC LIMIT 50"
    ),
    "ordinal in ORDER BY with LIMIT": (f"SELECT * FROM ({AGG}) t ORDER BY 4 DESC LIMIT 50"),
    "column alias list": (
        f"SELECT u AS user_id, ts AS label_ts, b FROM ({AGG}) t(u, ts, c, b) WHERE b > 0"
    ),
    "REPLACE to another name": (
        f"SELECT * FROM (SELECT * REPLACE (a AS churned) FROM ({AGG}) t) s WHERE churned > 0"
    ),
    "scalar subquery in an outer WHERE": (
        f"WITH t AS ({AGG}) SELECT l.user_id, l.label_ts, l.churned FROM labels l WHERE "
        "(SELECT t.a FROM t WHERE t.user_id = l.user_id AND t.label_ts = l.label_ts) > 0"
    ),
    "struct field": (
        f"SELECT * FROM (SELECT user_id, label_ts, churned, {{'v': a}} s FROM ({AGG}) t) q "
        "WHERE q.s.v > 0"
    ),
    "plain bound": EVENT_TIME_ONLY,
    "count star": (
        f"{SEL}, COUNT(*) n FROM labels l LEFT JOIN events e ON e.user_id = l.user_id "
        "GROUP BY 1, 2, 3"
    ),
}


@pytest.mark.parametrize("sql", SHAPES.values(), ids=SHAPES.keys())
def test_replay_adds_evidence_but_never_decides_the_label_or_the_row_caveats(sql, world, replayer):
    static = [f for f in suggest_fix(sql, SPEC, catalog=world) if f.rewrites]
    replayed = [f for f in suggest_fix(sql, SPEC, catalog=world, replay=replayer) if f.rewrites]
    assert [f.semantics for f in static] == [f.semantics for f in replayed]
    for before, after in zip(static, replayed, strict=True):
        assert set(before.caveats) <= set(after.caveats)  # replay only adds
    found_by_replay = [
        c
        for f in replayed
        for c in f.caveats
        if c.startswith(rewrite.ROWS_MAY_DROP) and "on the current data" in c
    ]
    warned_statically = any(c.startswith(rewrite.ROWS_MAY_DROP) for f in static for c in f.caveats)
    # a drop that replay's row count confirms must already have been said without replay
    assert warned_statically or not found_by_replay


def test_replay_compares_the_prediction_rows_not_only_how_many(world, replayer):
    """Fifty rows before, fifty after, but other rows: the key set shows it, the count does not."""
    sql = (
        f"{SEL}, SUM(e.amount) a FROM labels l LEFT JOIN events e ON e.user_id = l.user_id "
        "GROUP BY 1, 2, 3 ORDER BY MAX(e.amount) DESC LIMIT 50"
    )
    fix = [f for f in suggest_fix(sql, SPEC, catalog=world, replay=replayer) if f.rewrites][-1]
    text = " ".join(fix.caveats)
    assert "instead of" not in text  # the count is the same
    assert "no longer appear in the repaired query" in text
    assert fix.semantics is Semantics.CHANGED
