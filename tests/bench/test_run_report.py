"""The leakage benchmark counts and lists the cases a detector could not check."""

from bench.leakage.cases import LeakCase
from bench.leakage.run import DETECTORS, SYNTHETIC_SPEC, evaluate, markdown, score_cases
from sqlguard.leakage.spec import LeakageSpec

CLEAN = (
    "SELECT l.user_id, l.label_ts, l.churned, s.total_spend FROM labels l "
    "ASOF JOIN user_stats s ON l.user_id = s.user_id AND l.label_ts > s.updated_at"
)
CRASH = "SELECT l.user_id, l.label_ts, l.churned, l.no_such_column FROM labels l"
EVENT_TIME_ONLY = (
    "SELECT l.user_id, l.label_ts, l.churned, SUM(e.amount) AS amt FROM labels l "
    "JOIN events e ON e.user_id = l.user_id AND e.event_ts < l.label_ts GROUP BY 1, 2, 3"
)


def case(name: str, sql: str, leak_type: str) -> LeakCase:
    label = "clean" if leak_type == "clean" else "leaky"
    return LeakCase(name, name, "dev", label, leak_type, {}, sql)


def test_unverifiable_cases_are_counted_and_listed(world):
    catalog, _ = world
    spec = LeakageSpec.from_yaml(SYNTHETIC_SPEC)
    cases = [
        case("ok", CLEAN, "clean"),
        case("crash", CRASH, "clean"),
        case("hint", EVENT_TIME_ONLY, "L4"),
    ]
    scored = score_cases(cases, catalog, spec, seed=42)
    by_id = {s.case.case_id: s for s in scored}
    assert by_id["crash"].scores["static"] == 1.0  # an analysis crash still fails closed
    assert not by_id["ok"].verdicts

    report = evaluate(scored, {d: 0.5 for d in DETECTORS})
    assert report["detectors"]["static"]["n_unverifiable"] == 2  # crash + static-only L4 hint
    assert report["detectors"]["baseline"]["n_unverifiable"] == 0
    listed = {(u["case_id"], u["detector"]): u["reason"] for u in report["unverifiable_cases"]}
    assert "no_such_column" in listed[("crash", "static")]
    assert "settled by replay" in listed[("hint", "static")]
    assert ("ok", "static") not in listed and ("ok", "combined") not in listed


def test_markdown_lists_unverifiable_cases(world):
    catalog, _ = world
    spec = LeakageSpec.from_yaml(SYNTHETIC_SPEC)
    scored = score_cases([case("crash", CRASH, "clean")], catalog, spec, seed=42)
    split = evaluate(scored, {d: 0.5 for d in DETECTORS})
    result = {
        "git_sha": "abc",
        "prereg_sha": "abc",
        "splits": {"dev": split},
        "real_data": None,
        "optimism_gap": {},
    }
    text = markdown(result)
    assert "Unverifiable cases" in text and "`crash`" in text
