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


CUTOFF = "(SELECT MAX(label_ts) FROM labels)"


def cutoff_query(where: str, select: str = "COUNT(e.event_id) AS n") -> str:
    return f"""
    SELECT l.user_id, l.label_ts, l.churned, {select}
    FROM labels l LEFT JOIN events e ON e.user_id = l.user_id AND e.ingested_at < l.label_ts
    WHERE {where}
    GROUP BY ALL"""


@pytest.fixture(scope="module")
def final_conn(audit_world):
    return audit._connection(audit_world.final, RELATIONS)


@pytest.mark.parametrize(
    "where",
    [
        f"l.label_ts < {CUTOFF}",
        f"l.label_ts <= {CUTOFF} - INTERVAL 3 DAY",
        "l.label_ts < (SELECT MIN(label_ts) FROM labels) + INTERVAL 10 DAY",
        "l.label_ts < (SELECT quantile_cont(label_ts, 0.8) FROM labels)",
        "l.label_ts < (SELECT MEDIAN(label_ts) FROM labels)",
        f"l.label_ts BETWEEN (SELECT MIN(label_ts) FROM labels) AND {CUTOFF}",
        f"{CUTOFF} > l.label_ts",
    ],
)
def test_a_split_cutoff_is_fixed_to_its_final_value_and_the_query_is_audited(
    audit_world, points, final_conn, where
):
    sql = cutoff_query(where)
    fixed, values = audit.fix_spine_cutoffs(sql, final_conn, RELATIONS)
    assert values and "SELECT" not in fixed.split("WHERE")[1], fixed  # a literal replaces it
    result = judged(audit_world, points, "clean", sql)
    assert result.status == "agree", where  # audited, and consistent with its label
    assert any("split cutoff fixed at" in w for w in result.warnings)
    # not exempted: a wrong label on the same query is a disagreement
    assert judged(audit_world, points, "L3", sql).status == "disagree", where


@pytest.mark.parametrize(
    "name",
    ["q01_cutoff_plus_unbounded", "q03_cutoff_in_on", "q04_cutoff_on_events"],
)
def test_a_leak_next_to_a_cutoff_is_still_caught(audit_world, points, name):
    """Three leaky templates labelled clean, each with a spine subquery somewhere. The first
    carries a real cutoff and a leak beside it; the others use the subquery on `event_ts` and
    `ingested_at`, which is not a split cutoff."""
    sql = PLANTED[name]
    result = judged(audit_world, points, "clean", sql)
    assert result.status == "disagree" and "labelled clean" in result.reason, name


PLANTED = {
    "q01_cutoff_plus_unbounded": """
        SELECT l.user_id, l.label_ts, l.churned, count(e.event_id) AS n
        FROM labels l LEFT JOIN events e ON e.user_id = l.user_id
        WHERE l.label_ts <= (SELECT MAX(label_ts) FROM labels) GROUP BY ALL""",
    "q03_cutoff_in_on": """
        SELECT l.user_id, l.label_ts, l.churned, count(e.event_id) AS n
        FROM labels l LEFT JOIN events e ON e.user_id = l.user_id
          AND e.event_ts > (SELECT MIN(label_ts) FROM labels) GROUP BY ALL""",
    "q04_cutoff_on_events": """
        SELECT l.user_id, l.label_ts, l.churned, count(e.event_id) AS n
        FROM labels l LEFT JOIN events e ON e.user_id = l.user_id AND e.event_ts < l.label_ts
          AND e.ingested_at < (SELECT MAX(label_ts) FROM labels) GROUP BY ALL""",
}


@pytest.mark.parametrize(
    "sql",
    [
        # read as a feature, it is the future
        cutoff_query("l.user_id > 0", f"{CUTOFF} AS newest"),
        # pinned at the maximum it keeps rows that turned out to be the latest (a documented leak)
        cutoff_query(f"l.label_ts = {CUTOFF}"),
        # arithmetic inside the subquery is not the accepted form
        cutoff_query("l.label_ts < (SELECT MAX(label_ts) - INTERVAL 3 DAY FROM labels)"),
        # a cutoff that reads another table, or is filtered, is not a bare scan of the spine
        cutoff_query("l.label_ts < (SELECT MAX(event_ts) FROM events)"),
        cutoff_query("l.label_ts < (SELECT MAX(label_ts) FROM labels WHERE churned = 1)"),
        # correlated: not a split cutoff
        cutoff_query(
            "l.label_ts < (SELECT MAX(h.label_ts) FROM labels h WHERE h.user_id = l.user_id)"
        ),
        # compared with a column that is not the spine's label_ts
        cutoff_query("e.event_ts < (SELECT MAX(label_ts) FROM labels)"),
        cutoff_query("e.ingested_at >= (SELECT MIN(label_ts) FROM labels)"),
        cutoff_query("l.user_id < (SELECT MAX(label_ts) FROM labels)"),
    ],
)
def test_any_other_use_of_the_subquery_is_left_as_written(final_conn, sql):
    fixed, values = audit.fix_spine_cutoffs(sql, final_conn, RELATIONS)
    assert values == [] and fixed == sql


def test_the_cutoff_is_found_through_a_qualified_spine_name(final_conn):
    sql = cutoff_query(
        "l.label_ts < (SELECT MAX(label_ts) FROM analytics.marts.fct_training_spine)"
    )
    assert audit.fix_spine_cutoffs(sql, final_conn, {})[1] == []
    assert len(audit.fix_spine_cutoffs(sql, final_conn, RELATIONS)[1]) == 1


def test_the_cutoff_is_computed_once_from_the_final_spine(audit_world, final_conn):
    sql = cutoff_query("l.label_ts < (SELECT MIN(label_ts) FROM labels) + INTERVAL 10 DAY")
    fixed, [value] = audit.fix_spine_cutoffs(sql, final_conn, RELATIONS)
    earliest = min(audit_world.final["labels"].column("label_ts").to_pylist())
    assert value == earliest.isoformat(sep=" ") and f"CAST('{value}' AS TIMESTAMP)" in fixed
    assert "INTERVAL" in fixed  # the arithmetic outside the subquery stays in the query


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
        assert result.status == "not_auditable"
        assert result.reason.startswith(f"not auditable: {label}")


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
        "c13_time_split_cutoff": "agree",  # its split cutoffs are fixed, the rest is audited
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
    """Eight templates of each seen split (the full splits are audited in the report; the only
    disagreement left is the exact-14-day label boundary of hc07)."""
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


def test_a_clean_query_labelled_l6_is_not_judged_and_the_command_says_so(tmp_path, world, capsys):
    catalog, _ = world
    root = tmp_path / "root"
    root.mkdir()
    (root / "synthetic-42").symlink_to(
        Path(catalog.catalog.properties["warehouse"].removeprefix("file://"))
    )
    directory = tmp_path / "authored"
    write(directory, "hv2_l6_really_clean", "L6", EVENTS_BOUNDED)
    write(directory, "hv2_l7_really_clean", "L7", EVENTS_BOUNDED)
    assert (
        audit.main(["--candidate-dir", str(directory), "--warehouse", str(root), "--points", "6"])
        == 0
    )
    out = capsys.readouterr().out
    assert "not auditable by label: L6 1, L7 1" in out
    assert "not auditable hv2_l6_really_clean (L6): not auditable: L6" in out


def test_the_audit_command_imports_no_detector_module(tmp_path, world):
    """Running it loads the world generator and the SQL engine, never `sqlguard.leakage.*`."""
    import subprocess
    import sys

    catalog, _ = world
    root = tmp_path / "root"
    root.mkdir()
    (root / "synthetic-42").symlink_to(
        Path(catalog.catalog.properties["warehouse"].removeprefix("file://"))
    )
    directory = tmp_path / "authored"
    write(directory, "hv2_clean_ingest_bounded", "clean", EVENTS_BOUNDED)
    code = (
        "import sys\n"
        "from bench.leakage import audit\n"
        f"args = ['--candidate-dir', {str(directory)!r}, '--warehouse', {str(root)!r}]\n"
        "status = audit.main([*args, '--points', '4'])\n"
        "loaded = sorted(m for m in sys.modules if m.startswith('sqlguard.leakage'))\n"
        "print('LOADED', loaded)\n"
        "raise SystemExit(status)\n"
    )
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr[-400:]
    assert "LOADED []" in done.stdout, done.stdout[-400:]
    assert "bench.leakage.run" not in done.stdout
