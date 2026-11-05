"""The mechanical label audit: truth from the generator's load times, never from a detector."""

from pathlib import Path

import pytest
import yaml

from bench.leakage import audit
from bench.leakage.cases import Template, load_templates
from bench.leakage.splits import load_splits

RELATIONS = yaml.safe_load(load_splits()["holdout_v2"].spec.read_text(encoding="utf-8"))[
    "relations"
]
SPINE = "analytics.marts.fct_training_spine"
PLANS = "analytics.snapshots.plan_history_snapshot"

AS_OF_LOOKUP = f"""
SELECT l.user_id, l.label_ts, l.churned, p.plan
FROM {SPINE} AS l
LEFT JOIN {PLANS} AS p
  ON p.user_id = l.user_id AND p.ingested_at < l.label_ts AND p.valid_from < l.label_ts
 AND (p.valid_to IS NULL OR p.valid_to >= l.label_ts)
"""
NEWEST_LOADED = f"""
SELECT l.user_id, l.label_ts, l.churned, p.plan
FROM {SPINE} AS l
LEFT JOIN {PLANS} AS p ON p.user_id = l.user_id AND p.ingested_at < l.label_ts
QUALIFY row_number() OVER (PARTITION BY l.user_id, l.label_ts ORDER BY p.valid_from DESC) = 1
"""
LOAD_MARGIN = f"""
SELECT l.user_id, l.label_ts, l.churned, p.plan
FROM {SPINE} AS l
LEFT JOIN {PLANS} AS p ON p.user_id = l.user_id
 AND p.valid_from < l.label_ts - INTERVAL 8 DAY
 AND (p.valid_to IS NULL OR p.valid_to >= l.label_ts - INTERVAL 8 DAY)
"""
EVENTS_BOUNDED = """
SELECT l.user_id, l.label_ts, l.churned, COUNT(e.event_id) AS n
FROM labels l LEFT JOIN events e ON e.user_id = l.user_id AND e.ingested_at < l.label_ts
GROUP BY ALL
"""
EVENTS_EVENT_TIME_ONLY = """
SELECT l.user_id, l.label_ts, l.churned, COUNT(e.event_id) AS n
FROM labels l LEFT JOIN events e ON e.user_id = l.user_id AND e.event_ts < l.label_ts
GROUP BY ALL
"""
EVENTS_INCLUSIVE = """
SELECT l.user_id, l.label_ts, l.churned, COUNT(e.event_id) AS n
FROM labels l LEFT JOIN events e ON e.user_id = l.user_id AND e.ingested_at <= l.label_ts
GROUP BY ALL
"""
EVENTS_FUTURE_WINDOW = """
SELECT l.user_id, l.label_ts, l.churned, COUNT(e.event_id) AS n
FROM labels l LEFT JOIN events e
  ON e.user_id = l.user_id AND e.ingested_at < l.label_ts + INTERVAL 3 DAY
GROUP BY ALL
"""
EVENTS_UNBOUNDED = """
SELECT l.user_id, l.label_ts, l.churned, SUM(e.amount) AS total
FROM labels l LEFT JOIN events e ON e.user_id = l.user_id GROUP BY ALL
"""
LATEST_STATS_UNBOUNDED = """
SELECT l.user_id, l.label_ts, l.churned, s.total_spend
FROM labels l JOIN user_stats s ON s.user_id = l.user_id
QUALIFY row_number() OVER (PARTITION BY l.user_id, l.label_ts ORDER BY s.updated_at DESC) = 1
"""
STATS_ASOF = """
SELECT l.user_id, l.label_ts, l.churned, s.total_spend
FROM labels l ASOF LEFT JOIN user_stats s ON l.user_id = s.user_id AND l.label_ts > s.updated_at
"""
STATIC_COUNTRY = """
SELECT l.user_id, l.label_ts, l.churned, e.country
FROM labels l JOIN entities e ON e.user_id = l.user_id
"""


@pytest.fixture(scope="module")
def audit_world(world):
    catalog, _ = world
    return audit.AuditWorld.from_catalog(catalog)


@pytest.fixture(scope="module")
def points(audit_world):
    return audit_world.sample_points(audit.DEFAULT_POINTS, 42)


def template(label: str, sql: str, name: str = "t") -> Template:
    return Template(f"{name}_{label.lower()}", "candidate", label, {}, sql.strip())


def judged(audit_world, points, label, sql, relations=RELATIONS):
    [result] = audit.audit_templates([template(label, sql)], audit_world, relations, points)
    return result


# --- agreement and disagreement ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("sql", "label"),
    [
        (EVENTS_BOUNDED, "clean"),
        (STATS_ASOF, "clean"),
        (STATIC_COUNTRY, "clean"),
        (NEWEST_LOADED, "clean"),
        (LOAD_MARGIN, "clean"),
        (EVENTS_EVENT_TIME_ONLY, "L4"),
        (AS_OF_LOOKUP, "L4"),
        (EVENTS_INCLUSIVE, "L5"),
        (EVENTS_FUTURE_WINDOW, "L1"),
        (LATEST_STATS_UNBOUNDED, "L2"),
        (EVENTS_UNBOUNDED, "L3"),
    ],
)
def test_a_right_label_agrees(audit_world, points, sql, label):
    result = judged(audit_world, points, label, sql)
    assert (result.status, result.reason) == ("agree", "")


def test_the_as_of_lookup_labelled_clean_is_returned_with_the_row_that_shows_it(
    audit_world, points
):
    """The usual point-in-time idiom on `valid_to` used to be mislabelled clean: planted here."""
    result = judged(audit_world, points, "clean", AS_OF_LOOKUP)
    assert result.status == "disagree" and "labelled clean" in result.reason
    shown = result.evidence
    assert shown["rows_final"] != shown["rows_known_at_t"] or (
        shown["example_final"] != shown["example_known_at_t"]
    )
    assert shown["time"].startswith("2026-")


def test_an_event_time_bound_labelled_clean_is_a_disagreement(audit_world, points):
    assert judged(audit_world, points, "clean", EVENTS_EVENT_TIME_ONLY).status == "disagree"


def test_a_clean_query_labelled_leaky_is_a_disagreement(audit_world, points):
    result = judged(audit_world, points, "L2", EVENTS_BOUNDED)
    assert result.status == "disagree" and "no rendering differs" in result.reason


def test_the_label_type_is_not_judged_only_leaky_versus_clean(audit_world, points):
    assert judged(audit_world, points, "L5", EVENTS_UNBOUNDED).status == "agree"


def test_a_row_the_label_cannot_see_is_hidden(audit_world, points):
    """A feature copied from the row's own label differs: it is hidden as of `t`."""
    sql = "SELECT l.user_id, l.label_ts, l.churned, l.churned AS own FROM labels l"
    assert judged(audit_world, points, "clean", sql).status == "disagree"


def test_matured_labels_use_the_strict_boundary(audit_world, points):
    strict = """
    SELECT l.user_id, l.label_ts, l.churned,
           (SELECT AVG(h.churned) FROM labels h
             WHERE h.label_ts < l.label_ts - INTERVAL 14 DAY) AS rate
    FROM labels l"""
    inclusive = strict.replace("h.label_ts < l.label_ts -", "h.label_ts <= l.label_ts -")
    assert judged(audit_world, points, "clean", strict).status == "agree"
    assert judged(audit_world, points, "clean", inclusive).status == "disagree"


def test_a_future_spine_row_is_not_known(audit_world, points):
    sql = """
    SELECT l.user_id, l.label_ts, l.churned, (SELECT MAX(label_ts) FROM labels) AS newest
    FROM labels l"""
    assert judged(audit_world, points, "L1", sql).status == "agree"
    assert judged(audit_world, points, "clean", sql).status == "disagree"


# --- what it will not decide --------------------------------------------------------------------


def test_l6_and_l7_are_not_auditable_with_the_reason(audit_world, points):
    for label in ("L6", "L7"):
        result = judged(audit_world, points, label, EVENTS_BOUNDED)
        assert result.status == "not_auditable" and result.reason.startswith(label)


@pytest.mark.parametrize("label", ["clean", "L3"])
def test_a_query_that_reads_the_wall_clock_is_not_auditable(audit_world, points, label):
    sql = """
    SELECT l.user_id, l.label_ts, l.churned, date_diff('day', e.signup_ts, now()) AS age
    FROM labels l JOIN entities e ON e.user_id = l.user_id"""
    result = judged(audit_world, points, label, sql)
    assert result.status == "not_auditable" and "wall clock" in result.reason


def test_a_comment_or_literal_mentioning_now_is_not_a_clock_read():
    sql = "SELECT 'now' AS w FROM labels -- now() is not used"
    assert not audit.reads_the_clock(sql)
    assert audit.reads_the_clock("SELECT current_date AS d FROM labels")


def test_a_query_that_does_not_run_is_not_auditable(audit_world, points):
    result = judged(audit_world, points, "clean", "SELECT nope FROM labels")
    assert result.status == "not_auditable" and "does not run" in result.reason


def test_an_output_without_features_or_keys_is_not_auditable(audit_world, points):
    only_spine = "SELECT user_id, label_ts, churned FROM labels"
    assert "no feature column" in judged(audit_world, points, "clean", only_spine).reason
    no_keys = "SELECT COUNT(*) AS n FROM labels"
    assert "lacks user_id or label_ts" in judged(audit_world, points, "clean", no_keys).reason


def test_too_little_output_is_not_a_clean_verdict(audit_world, points):
    sql = """
    SELECT l.user_id, l.label_ts, l.churned, e.country
    FROM labels l JOIN entities e ON e.user_id = l.user_id WHERE l.user_id = 1"""
    result = judged(audit_world, points, "clean", sql)
    assert result.status == "not_auditable" and "sampled times" in result.reason


def test_a_mixed_template_warns(audit_world, points):
    body = """
    SELECT l.user_id, l.label_ts, l.churned, COUNT(e.event_id) AS n
    FROM labels l LEFT JOIN events e ON e.user_id = l.user_id
     AND e.ingested_at < l.label_ts + INTERVAL '{shift} days' GROUP BY ALL"""
    mixed = Template("mixed", "candidate", "L1", {"shift": ["0", "3"]}, body.strip())
    [result] = audit.audit_templates([mixed], audit_world, RELATIONS, points)
    assert result.status == "agree" and any("no leak" in w for w in result.warnings)


# --- the sample and the comparison ---------------------------------------------------------------


def test_every_prediction_time_is_audited_by_default(audit_world):
    assert audit.DEFAULT_POINTS == 0
    assert audit_world.sample_points(audit.DEFAULT_POINTS, 1) == audit_world.prediction_times()


def test_the_sample_is_deterministic_and_covers_midnights(audit_world):
    a, b = audit_world.sample_points(24, 7), audit_world.sample_points(24, 7)
    assert a == b and len(a) == 24 and a == sorted(a)
    assert any(t.hour == t.minute == t.second == 0 for t in a)
    assert len(audit_world.sample_points(0, 7)) == len(audit_world.prediction_times())


def test_known_at_t_follows_the_load_times(audit_world):
    from datetime import datetime

    t = datetime(2026, 1, 20, 6)
    known = audit_world.known_at(t)
    assert max(known["events"].column("ingested_at").to_pylist()) < t
    assert max(known["user_stats"].column("updated_at").to_pylist()) < t
    assert known["user_outcomes"].num_rows == 0
    assert max(known["labels"].column("label_ts").to_pylist()) <= t
    assert all(
        row["churned"] is None or row["label_ts"] + audit_world.horizon < t
        for row in known["labels"].to_pylist()
    )
    assert known["entities"].num_rows == audit_world.final["entities"].num_rows


def test_float_noise_is_not_a_difference():
    assert audit.same_rows([(1, 24.856562)], [(1, 24.856562000000003)])
    assert not audit.same_rows([(1, 24.85)], [(1, 24.86)])
    assert not audit.same_rows([(1, None)], [(1, 1.0)])
    assert audit.same_rows([(1, float("nan"))], [(1, float("nan"))])


def test_the_audit_uses_no_detector_and_no_snapshot():
    source = Path(audit.__file__).read_text(encoding="utf-8")
    assert "sqlguard.leakage" not in source
    assert "snapshot_as_of" not in source and "Replayer" not in source


# --- the audit against the templates we already trust --------------------------------------


SEEN_SAMPLE = {
    "dev": {
        "c13_time_split_cutoff": "disagree",  # a cutoff read as MAX(label_ts): the future spine
        "c01_asof_stats": "agree",
        "c04_cte_events": "agree",
        "l1_future_window": "agree",
        "l3_total_cte": "agree",
        "l4_event_time_window": "agree",
        "l5_ingest_inclusive": "agree",
        "l7_clock_bound": "not_auditable",
    },
    "dev_v1b": {
        # a label counted as final exactly `horizon` old, where the strict boundary says not yet
        "hc07_target_encoding_matured": "disagree",
        "hc01_lateral_recent": "agree",
        "hc04_validity_join": "agree",
        "h1_future_asof": "agree",
        "h2_scd2_current": "agree",
        "h4_exists_event_time": "agree",
        "h5_qualify_inclusive": "agree",
        "h6_exists_outcome": "not_auditable",
    },
}


@pytest.mark.parametrize("split", SEEN_SAMPLE)
def test_the_audit_agrees_with_the_seen_splits_except_at_two_edges(audit_world, points, split):
    """Eight templates of each seen split (the full splits are audited in the report: 46 of 56 and
    40 of 46 agree, 9 and 5 are not auditable, and exactly these two disagree)."""
    wanted = SEEN_SAMPLE[split]
    templates = [t for t in load_templates(split) if t.template_id in wanted]
    assert {t.template_id for t in templates} == set(wanted)
    results = audit.audit_templates(templates, audit_world, {}, points)
    assert {r.template_id: r.status for r in results} == wanted


# --- the command line ---------------------------------------------------------------------------


def write(directory: Path, name: str, label: str, sql: str) -> None:
    directory.mkdir(exist_ok=True)
    (directory / f"{name}.sql").write_text(f"-- leak: {label}\n{sql.strip()}\n", encoding="utf-8")


def test_the_command_exits_1_on_a_planted_mislabel(tmp_path, world, capsys):
    catalog, _ = world
    warehouse = Path(catalog.catalog.properties["warehouse"].removeprefix("file://"))
    seed = 42
    # the benchmark reads `<warehouse>/synthetic-<seed>`; the session world lives in `warehouse`
    root = tmp_path / "root"
    root.mkdir()
    (root / f"synthetic-{seed}").symlink_to(warehouse)
    directory = tmp_path / "authored"
    write(directory, "hv2_clean_ingest_bounded", "clean", EVENTS_BOUNDED)
    write(directory, "hv2_clean_as_of_lookup", "clean", AS_OF_LOOKUP)
    args = ["--candidate-dir", str(directory), "--warehouse", str(root), "--points", "24"]

    assert audit.main(args) == 1
    out = capsys.readouterr().out
    assert "DISAGREE hv2_clean_as_of_lookup (clean)" in out
    assert "hv2_clean_ingest_bounded" not in out.split("DISAGREE")[1]

    (directory / "hv2_clean_as_of_lookup.sql").unlink()
    assert audit.main([*args, "--out", str(tmp_path / "audit.json")]) == 0
    assert (tmp_path / "audit.json").read_text().count("hv2_clean_ingest_bounded") >= 1

    empty = tmp_path / "empty"
    empty.mkdir()
    assert audit.main(["--candidate-dir", str(empty)]) == 2
