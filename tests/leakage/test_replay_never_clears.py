"""A replay result never lowers a static score. When replay found no difference it is attached to
the findings it is about as information, with how many later rows it hid; the verdict is the
static one."""

from dataclasses import replace

import pytest

from bench.leakage.world import WorldConfig, build_world
from sqlguard.core.catalog import LakehouseCatalog
from sqlguard.leakage.fixes import suggest_fix
from sqlguard.leakage.replay import Replayer, ReplayResult, ReplayStatus
from sqlguard.leakage.spec import LeakageSpec
from sqlguard.leakage.validator import validate

from .conftest import SCHEMA, SPEC

SEL = "SELECT l.user_id, l.label_ts, l.churned"
SPEC_V3 = LeakageSpec.from_dict({**SPEC, "max_ingest_delay": {"events": "8 days"}})
BENCH_SPEC = LeakageSpec.from_yaml("bench/leakage/spec.yaml")

ALL_TABLES = ["events", "user_stats", "labels"]
PASS = ReplayResult(
    ReplayStatus.PASS,
    points_checked=8,
    rows_compared=120,
    timestamps_compared=8,
    exposure={t: 400 for t in ALL_TABLES},
    exposed_points={t: 8 for t in ALL_TABLES},
)
VACUOUS_PASS = replace(PASS, exposure={t: 0 for t in ALL_TABLES}, exposed_points={})
MISMATCH = ReplayResult(
    ReplayStatus.MISMATCH,
    points_checked=8,
    rows_compared=120,
    rows_mismatched=9,
    mismatched_columns={"a": 9},
    timestamps_compared=8,
)
TOO_LITTLE = ReplayResult(
    ReplayStatus.UNVERIFIABLE, reason="needs at least 20 rows", insufficient_evidence=True
)


class Fixed(Replayer):
    """A replayer that returns one result, whatever the query."""

    def __init__(self, result: ReplayResult) -> None:
        self.result = result

    def run(self, sql: str) -> ReplayResult:
        return self.result


QUERIES = {
    "L1": (
        "SELECT l.user_id, l.label_ts, l.churned, s.total_spend FROM labels l "
        "JOIN user_stats s ON s.user_id = l.user_id AND s.updated_at < l.label_ts + INTERVAL 2 DAY"
    ),
    "L2": f"{SEL}, e.amount FROM labels l LEFT JOIN events e ON e.user_id = l.user_id",
    "L3": (
        f"{SEL}, SUM(e.amount) AS a FROM labels l LEFT JOIN events e ON e.user_id = l.user_id "
        "GROUP BY 1, 2, 3"
    ),
    "L5": (
        f"{SEL}, SUM(e.amount) AS a FROM labels l LEFT JOIN events e ON e.user_id = l.user_id "
        "AND e.ingested_at <= l.label_ts GROUP BY 1, 2, 3"
    ),
}


def run(sql, result, spec=SPEC_V3):
    return validate(sql, spec, schema=SCHEMA, replay=Fixed(result))


@pytest.mark.parametrize("rule", QUERIES)
@pytest.mark.parametrize("result", [PASS, VACUOUS_PASS], ids=["exposed pass", "vacuous pass"])
def test_a_replay_pass_never_lowers_the_score_or_the_verdict(rule, result):
    static = validate(QUERIES[rule], SPEC_V3, schema=SCHEMA)
    report = run(QUERIES[rule], result)
    assert static.verdict == report.verdict == "leaky"
    assert report.score == static.score and report.leak_types == static.leak_types
    assert rule in report.leak_types


@pytest.mark.parametrize("rule", QUERIES)
def test_what_a_pass_showed_is_attached_to_the_finding_as_information(rule):
    [finding] = [f for f in run(QUERIES[rule], PASS).findings if f.rule_id == rule]
    assert finding.evidence["replay_no_mismatch"] is True and finding.evidence["replay_informative"]
    assert finding.evidence["replay_exposed_rows"] == 400
    assert finding.evidence["replay_exposed_points"] == 8
    assert "does not change the score" in finding.message and "no mismatch" in finding.message

    [thin] = [f for f in run(QUERIES[rule], VACUOUS_PASS).findings if f.rule_id == rule]
    assert thin.evidence["replay_informative"] is False
    assert "too few to say anything" in thin.message


@pytest.mark.parametrize("rule", QUERIES)
@pytest.mark.parametrize("result", [MISMATCH, TOO_LITTLE], ids=["mismatch", "too little"])
def test_a_replay_that_did_not_pass_adds_nothing_to_the_findings(rule, result):
    report = run(QUERIES[rule], result)
    assert report.verdict == "leaky"
    assert not any("replay_no_mismatch" in f.evidence for f in report.findings)


def test_the_report_dict_only_gained_replay_fields():
    data = run(QUERIES["L2"], PASS).to_dict()
    assert set(data) == {"verdict", "score", "leak_types", "findings", "replay"}
    assert data["replay"]["exposure"]["events"] == 400


def test_fixes_are_still_suggested_for_a_finding_replay_did_not_object_to():
    report = run(QUERIES["L2"], PASS)
    assert suggest_fix(QUERIES["L2"], SPEC_V3, report=report, schema=SCHEMA)


# --- leaks a sampled replay misses stay leaky (found by review against the oracle) ---------------

LEAKS_REPLAY_MISSES = {
    # 2 of 409 output rows differ from point-in-time truth; a sample may miss them
    "one user reads three days ahead": f"""{SEL}, COALESCE(SUM(e.amount), 0) AS spend
FROM labels l LEFT JOIN events e ON e.user_id = l.user_id
 AND (e.ingested_at < l.label_ts OR (l.user_id = 7 AND e.ingested_at < l.label_ts + INTERVAL 3 DAY))
GROUP BY 1, 2, 3""",
    # an explicit read of rows loaded 40 days after prediction time (none exist in the world yet)
    "explicit future read": f"""{SEL}, COALESCE(SUM(e.amount), 0) AS later FROM labels l
LEFT JOIN events e ON e.user_id = l.user_id AND e.ingested_at > l.label_ts + INTERVAL 40 DAY
GROUP BY 1, 2, 3""",
    # every event of the user, past and future, filtered on a value no row has yet
    "unbounded rare slice": f"""{SEL}, COUNT(e.event_id) AS n FROM labels l LEFT JOIN events e
 ON e.user_id = l.user_id AND e.amount > 100000 GROUP BY 1, 2, 3""",
}


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    catalog = LakehouseCatalog.local(tmp_path_factory.mktemp("never-clears-world"))
    build_world(catalog, WorldConfig(seed=42))
    return catalog


@pytest.mark.parametrize("seed", [42, 0, 7])
@pytest.mark.parametrize("sql", LEAKS_REPLAY_MISSES.values(), ids=LEAKS_REPLAY_MISSES.keys())
def test_a_sampled_replay_pass_does_not_clear_a_static_leak(world, sql, seed):
    report = validate(sql, BENCH_SPEC, catalog=world, replay=Replayer(world, BENCH_SPEC, seed=seed))
    assert report.verdict != "clean", [f.rule_id for f in report.findings]
