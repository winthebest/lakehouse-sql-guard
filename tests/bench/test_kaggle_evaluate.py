"""Pure parts of bench/kaggle/evaluate.py: conversions, outcome parsing, scoring, metrics."""

import hashlib
import json
from pathlib import Path

import pytest
import yaml

from bench.kaggle import evaluate as ev
from bench.kaggle.evaluate import ModeResult, Query, Record

ROOT = Path(__file__).resolve().parents[2]
THRESHOLDS = {"static": 0.425, "replay": 0.004, "combined": 0.475}


def mode(cls, score=None, types=(), status=None, compared=0, mismatched=0, detail=""):
    return ModeResult(cls, score, tuple(types), status, compared, mismatched, detail)


def record(case, label, static, replay, leak_type=None, also=(), disputed=False, name="q.sql"):
    return Record(Query(case, name, label, leak_type, tuple(also), 1, disputed), static, replay)


# --- schema conversion --------------------------------------------------------------------------


def test_convert_schema_flattens_columns_and_drops_the_rest():
    schema = {
        "tables": {
            "train": {
                "columns": {"b": "BIGINT", "a": "DECIMAL(10, 2)"},
                "event_time": "b",
                "time_unit": "s",
                "availability_time": None,
            },
            "people": {"columns": {"id": "VARCHAR"}},
        },
        "spine": {"table": "train"},
    }
    converted = ev.convert_schema(schema)
    assert converted == {
        "train": {"b": "BIGINT", "a": "DECIMAL(10, 2)"},
        "people": {"id": "VARCHAR"},
    }
    assert list(converted["train"]) == ["b", "a"]  # column order is kept


@pytest.mark.parametrize(
    "schema", [{}, {"tables": {}}, {"tables": {"t": {}}}, {"tables": {"t": {"columns": {}}}}]
)
def test_convert_schema_rejects_what_it_cannot_convert(schema):
    with pytest.raises(ev.ConversionError):
        ev.convert_schema(schema)


def test_convert_schema_reads_the_checker_format_for_every_case():
    for case in yaml.safe_load((ROOT / "bench/kaggle/eval-prereg.yaml").read_text())["inputs"][
        "cases"
    ]:
        schema = yaml.safe_load((ROOT / "bench/kaggle/cases" / case / "schema.yaml").read_text())
        converted = ev.convert_schema(schema)
        assert set(converted) == set(schema["tables"])
        assert all(isinstance(t, str) for cols in converted.values() for t in cols.values())


# --- types and times ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sql", "arrow"),
    [
        ("TINYINT", "int32"),
        ("smallint", "int32"),
        ("INTEGER", "int32"),
        ("BIGINT", "int64"),
        ("FLOAT", "float"),
        ("DOUBLE", "double"),
        ("BOOLEAN", "bool"),
        ("VARCHAR", "string"),
        ("DATE", "date32[day]"),
        ("TIMESTAMP", "timestamp[us]"),
        ("DECIMAL(10, 2)", "decimal128(10, 2)"),
    ],
)
def test_arrow_type(sql, arrow):
    assert str(ev.arrow_type(sql)) == arrow


def test_arrow_type_rejects_unknown_types():
    with pytest.raises(ev.BuildError, match="unsupported"):
        ev.arrow_type("JSON")


def test_to_datetime():
    from datetime import date, datetime

    assert ev.to_datetime(datetime(2020, 1, 2, 3), "TIMESTAMP", None) == datetime(2020, 1, 2, 3)
    assert ev.to_datetime(date(2020, 1, 2), "DATE", None) == datetime(2020, 1, 2)
    assert ev.to_datetime(86_400, "BIGINT", "s") == datetime(1970, 1, 2)
    assert ev.to_datetime(1500, "BIGINT", "ms") == datetime(1970, 1, 1, 0, 0, 1, 500_000)


@pytest.mark.parametrize(
    ("value", "sql_type", "unit", "message"),
    [
        (None, "TIMESTAMP", None, "null"),
        (5, "BIGINT", None, "time_unit"),
        (5, "BIGINT", "min", "time_unit"),
        (10**18, "BIGINT", "s", "out of range"),
        ("x", "VARCHAR", None, "cannot read"),
    ],
)
def test_to_datetime_refuses(value, sql_type, unit, message):
    with pytest.raises(ev.BuildError, match=message):
        ev.to_datetime(value, sql_type, unit)


# --- commit plan --------------------------------------------------------------------------------


def test_table_roles():
    schema = {
        "tables": {"Train": {}, "people": {}, "outcomes": {}, "events": {}},
        "spine": {"table": "train"},
    }
    spec = {"static_tables": ["People"], "outcome_columns": ["outcomes.refund"]}
    assert ev.table_roles(schema, spec) == {
        "Train": "label",
        "people": "static",
        "outcomes": "outcome",
        "events": "temporal",
    }


def test_plan_commits_orders_by_load_time():
    from datetime import datetime, timedelta

    d = datetime(2026, 3, 10)
    roles = {"people": "static", "events": "temporal", "labels": "label", "refunds": "outcome"}
    event = {
        "events": [d + timedelta(hours=1), d + timedelta(hours=2), d + timedelta(hours=3)],
        "labels": [d + timedelta(days=1), d + timedelta(days=2)],
        "refunds": [d + timedelta(days=3)],
    }
    load = {"events": [d + timedelta(days=2), d + timedelta(days=1), d + timedelta(days=2)]}
    plans = ev.plan_commits(roles, event, load)
    assert [(p.table, p.logical_ts, p.rows) for p in plans] == [
        ("people", d + timedelta(hours=1) - ev.DAY, None),
        ("events", d + timedelta(days=1), (1,)),
        ("events", d + timedelta(days=2), (0, 2)),
        ("labels", d + timedelta(days=3) + ev.DAY, None),
        ("refunds", d + timedelta(days=3) + ev.DAY, None),
    ]


def test_plan_commits_needs_an_event_time():
    with pytest.raises(ev.BuildError):
        ev.plan_commits({"people": "static"}, {}, {})


# --- parsing the CLI ----------------------------------------------------------------------------


def cli_json(**entry):
    base = {"file": "q.sql", "verdict": "clean", "score": 0.0, "leak_types": [], "findings": []}
    return json.dumps([{**base, **entry}])


def test_parse_leaky_with_replay_mismatch():
    out = cli_json(
        verdict="leaky",
        score=0.97,
        leak_types=["L1", "L6"],
        replay={"status": "mismatch", "rows_compared": 40, "rows_mismatched": 8, "reason": ""},
    )
    assert ev.parse_cli_output(out, "", 1) == mode("leaky", 0.97, ["L1", "L6"], "mismatch", 40, 8)


def test_parse_unverifiable_names_the_unchecked_finding_first():
    findings = [
        {"rule_id": "L3", "message": "aggregate"},
        {"rule_id": "UNCLASSIFIED", "message": "table 'x' is not declared"},
    ]
    out = cli_json(verdict="unverifiable", score=0.2, findings=findings)
    parsed = ev.parse_cli_output(out, "", 1)
    assert parsed.cls == "unverifiable"
    assert parsed.detail == "UNCLASSIFIED: table 'x' is not declared"


def test_parse_unverifiable_falls_back_to_the_replay_reason():
    out = cli_json(verdict="unverifiable", replay={"status": "unverifiable", "reason": "too few"})
    assert ev.parse_cli_output(out, "", 1).detail == "too few"


@pytest.mark.parametrize("stdout", ["", "not json", "[]", "{}", '[{"verdict": "maybe"}]', "[1]"])
def test_parse_anything_else_is_an_error(stdout):
    parsed = ev.parse_cli_output(stdout, "error: spec is broken\n", 2)
    assert parsed.cls == "error" and parsed.score is None
    assert parsed.detail == "error: spec is broken"


def test_error_summary_names_the_deepest_sqlguard_frame():
    stderr = "\n".join(
        [
            "Traceback (most recent call last):",
            '  File "/x/site-packages/yaml/__init__.py", line 3, in load',
            '  File "/x/src/sqlguard/leakage/spec.py", line 130, in __post_init__',
            '  File "/x/src/sqlguard/leakage/spec.py", line 150, in from_dict',
            "sqlguard.leakage.spec.SpecError: label_table must not also be declared temporal",
        ]
    )
    assert ev.error_summary(stderr) == (
        "sqlguard.leakage.spec.SpecError: label_table must not also be declared temporal"
        " [spec.py:150 from_dict]"
    )


def test_error_summary_without_output():
    assert ev.error_summary("", "", 3) == "exit code 3 with no output"
    assert ev.error_summary("", "partial\n", 1) == "partial"


def test_run_check_on_a_leaky_example():
    example = ROOT / "examples" / "leakage"
    result = ev.run_check(
        example / "sql" / "tickets_total.sql",
        example / "spec.yaml",
        example / "schema.yaml",
        None,
        timeout=120,
    )
    assert result.cls == "leaky" and result.score >= 0.5
    assert "L2" in result.leak_types or "L3" in result.leak_types


def test_run_check_reports_a_rejected_spec_as_an_error(tmp_path):
    spec = tmp_path / "spec.yaml"
    spec.write_text("label_table: t\nentity_keys: []\nprediction_ts: ts\nlabel_columns: [y]\n")
    example = ROOT / "examples" / "leakage"
    result = ev.run_check(
        example / "sql" / "tickets_total.sql", spec, example / "schema.yaml", None, timeout=120
    )
    assert result.cls == "error"
    assert "missing required fields" in result.detail


# --- scoring ------------------------------------------------------------------------------------


def test_replay_score_is_the_mismatch_rate_only_on_a_mismatch():
    assert ev.replay_score(mode("leaky", 1.0, status="mismatch", compared=50, mismatched=5)) == 0.1
    assert ev.replay_score(mode("clean", 0.0, status="pass", compared=50)) == 0.0
    assert ev.replay_score(mode("unverifiable", 1.0, status="unverifiable")) == 0.0
    assert ev.replay_score(mode("error")) is None
    assert ev.replay_score(mode("static_only")) is None


def test_flagged_per_rule():
    r = record(
        "a",
        "leaky",
        static=mode("leaky", 0.5),
        replay=mode("clean", 0.4, status="mismatch", compared=100, mismatched=1),
    )
    assert ev.flagged(r, "verdict:static", THRESHOLDS)
    assert not ev.flagged(r, "verdict:combined", THRESHOLDS)
    assert ev.flagged(r, "threshold:static", THRESHOLDS)
    assert ev.flagged(r, "threshold:replay", THRESHOLDS)  # 0.01 >= 0.004
    assert not ev.flagged(r, "threshold:combined", THRESHOLDS)  # 0.4 < 0.475


def test_a_threshold_counts_an_unverifiable_score_unless_strict():
    r = record("a", "leaky", static=mode("unverifiable", 1.0), replay=mode("unverifiable", 1.0))
    assert ev.flagged(r, "threshold:static", THRESHOLDS)
    assert not ev.flagged(r, "threshold:static", THRESHOLDS, strict=True)
    assert not ev.flagged(r, "verdict:static", THRESHOLDS)


def test_an_error_has_no_score_and_is_never_detected():
    r = record("a", "leaky", static=mode("error"), replay=mode("static_only"))
    for rule in ev.RULES:
        assert not ev.flagged(r, rule, THRESHOLDS)


def test_blocked_counts_undecided_runs():
    clean = record("a", "clean", static=mode("unverifiable", 0.1), replay=mode("clean", 0.0))
    assert ev.blocked(clean, "verdict:static", THRESHOLDS)
    assert not ev.blocked(clean, "verdict:combined", THRESHOLDS)
    errored = record("a", "clean", static=mode("error"), replay=mode("error"))
    assert ev.blocked(errored, "threshold:combined", THRESHOLDS)


def sample_records():
    hit = mode("leaky", 0.97, ["L6"])
    miss = mode("clean", 0.0)
    return [
        record("kl01", "leaky", hit, hit, "L6", name="l1.sql"),
        record("kl01", "clean", miss, miss, name="c1.sql"),
        record("kl02", "leaky", hit, miss, "L1", ["L6"], name="l2.sql"),
        record("kl02", "clean", mode("leaky", 0.9, ["L3"]), miss, name="c2.sql"),
        record("kl03", "leaky", mode("error"), mode("error"), "L1", disputed=True, name="l3.sql"),
        record("kl03", "clean", mode("error"), mode("error"), disputed=True, name="c3.sql"),
        record("kl04", "leaky", mode("unverifiable", 1.0), miss, "L5", name="l4.sql"),
        record("kl04", "clean", miss, miss, name="c4.sql"),
    ]


def metrics(rule):
    return ev.rule_metrics(sample_records(), rule, THRESHOLDS, seed=42, resamples=200, level=0.95)


def test_rule_metrics_by_hand():
    m = metrics("verdict:static")
    assert (m["n_leaky"], m["n_clean"]) == (4, 4)
    assert m["recall"] == {"k": 2, "n": 4, "rate": 0.5}
    assert m["fpr"] == {"k": 1, "n": 4, "rate": 0.25}
    assert m["recall_strict"]["k"] == 2
    # clean: the false alarm, the errored clean query and the one that is merely not flagged
    assert m["block_rate_clean"] == {"k": 2, "n": 4, "rate": 0.5}
    assert m["recall_decided"] == {"k": 2, "n": 2, "rate": 1.0}  # kl03 and kl04 are undecided
    assert m["fpr_decided"] == {"k": 1, "n": 3, "rate": 0.3333}


def test_rule_metrics_strict_differs_for_an_unverifiable_score_over_the_threshold():
    m = metrics("threshold:static")
    assert m["recall"]["k"] == 3  # kl04's unverifiable 1.0 reaches 0.425
    assert m["recall_strict"]["k"] == 2


def test_rule_metrics_by_type_and_attribution():
    m = metrics("verdict:static")
    assert m["by_leak_type"]["L6"]["as_scored"] == {"k": 1, "n": 1, "rate": 1.0}
    assert m["by_leak_type"]["L1"]["as_scored"]["k"] == 1
    assert m["by_leak_type"]["L5"]["as_scored"]["k"] == 0
    # the L1 query is reported as L6: only the also_matches reading credits it
    assert m["attribution"] == {"detected": 2, "primary_type": 1, "primary_or_also_matches": 2}


def test_rule_metrics_intervals_are_clustered_and_seeded():
    first, second = metrics("verdict:static"), metrics("verdict:static")
    assert first["recall_ci95"] == second["recall_ci95"]
    low, high = first["recall_ci95"]
    assert 0.0 <= low <= 0.5 <= high <= 1.0
    all_error = ev.rule_metrics(
        [r for r in sample_records() if r.query.case in ("kl03", "kl04")],
        "verdict:combined",
        THRESHOLDS,
        seed=42,
        resamples=50,
        level=0.95,
    )
    assert all_error["recall"]["k"] == 0 and all_error["recall_ci95"] == [0.0, 0.0]


def test_class_counts_problem_groups_and_per_case():
    records = sample_records()
    counts = ev.class_counts(records)
    assert counts["static"]["leaky"] == {
        "leaky": 2,
        "clean": 0,
        "unverifiable": 1,
        "error": 1,
        "static_only": 0,
    }
    assert counts["replay"]["clean"]["error"] == 1
    groups = ev.problem_groups(records)
    assert [(g["mode"], g["class"], g["queries"], g["cases"]) for g in groups] == [
        ("replay", "error", 2, ["kl03"]),
        ("static", "error", 2, ["kl03"]),
        ("static", "unverifiable", 1, ["kl04"]),
    ]
    cases = ev.per_case(records, THRESHOLDS)
    assert cases["kl03"]["disputed"] and not cases["kl01"]["disputed"]
    assert cases["kl02"]["flagged"]["verdict:static"]["clean"]["k"] == 1


def test_csv_rows_has_one_row_per_query_and_mode():
    rows = ev.csv_rows(sample_records())
    assert len(rows) == 16
    assert set(rows[0]) == set(ev.CSV_COLUMNS)
    assert {r["mode"] for r in rows} == {"static", "replay"}


def test_report_renders_from_a_summary():
    records = sample_records()
    summary = ev.summarize(records, THRESHOLDS, 42, 100, 0.95)
    result = {
        "meta": {
            "stem": "20261107-abcdef0",
            "git_sha": "abcdef0",
            "git_dirty": False,
            "prereg_commit": "1234567",
            "cases_digest": "0" * 64,
            "checker": {"release_tag": "v0.2.0", "src_tree": "1" * 40},
            "thresholds": THRESHOLDS,
            "verdict_threshold": 0.5,
            "resamples": 100,
            "seed": 42,
        },
        "all": summary,
        "without_disputed": ev.summarize(
            [r for r in records if not r.query.disputed], THRESHOLDS, 42, 100, 0.95
        ),
        "per_case": ev.per_case(records, THRESHOLDS),
        "problems": ev.problem_groups(records),
        "build": {
            c: {"status": "built", "reason": "", "rows": {"t": 3}, "commits": {"t": 1}}
            for c in summary["cases"]
        },
    }
    text = ev.render_markdown(result)
    assert "## All 14 cases" in text and "## Without kl05, kl09, kl14" in text
    assert "| verdict:static | 2/4 = 0.50" in text
    assert "kl03" in text


# --- the registration and the cases -------------------------------------------------------------


def test_cases_digest_depends_on_content_not_on_order_or_pycache(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "x.txt").write_text("1")
    (tmp_path / "b.txt").write_text("2")
    first = ev.cases_digest(tmp_path)
    assert first[1] == 2
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "junk.pyc").write_text("zz")
    assert ev.cases_digest(tmp_path) == first
    (tmp_path / "b.txt").write_text("3")
    assert ev.cases_digest(tmp_path)[0] != first[0]
    expected = "".join(
        f"{rel}\t{hashlib.sha256(data.encode()).hexdigest()}\n"
        for rel, data in (("a/x.txt", "1"), ("b.txt", "3"))
    )
    assert ev.cases_digest(tmp_path)[0] == hashlib.sha256(expected.encode()).hexdigest()


def test_the_cases_are_the_registered_ones():
    prereg = yaml.safe_load((ROOT / "bench/kaggle/eval-prereg.yaml").read_text())
    digest, count = ev.cases_digest()
    assert digest == prereg["inputs"]["cases_digest"]
    assert count == prereg["inputs"]["cases_file_count"]


def test_registered_thresholds_are_the_frozen_ones():
    prereg = yaml.safe_load((ROOT / "bench/kaggle/eval-prereg.yaml").read_text())
    source = ROOT / prereg["thresholds"]["source"]
    assert hashlib.sha256(source.read_bytes()).hexdigest() == prereg["thresholds"]["source_sha256"]
    frozen = json.loads(source.read_text())["thresholds"]
    for name in ("static", "replay", "combined"):
        assert prereg["thresholds"][name] == frozen[name]


def test_the_queries_are_80_with_40_leaky():
    prereg = yaml.safe_load((ROOT / "bench/kaggle/eval-prereg.yaml").read_text())
    queries = [
        q
        for case in prereg["inputs"]["cases"]
        for q in ev.load_queries(ev.CASES_DIR / case, prereg["disputed_labels"]["cases"])
    ]
    assert len(queries) == 80 and sum(q.is_leaky for q in queries) == 40
    assert {q.case for q in queries if q.disputed} == {"kl05", "kl09", "kl14"}
    assert all(q.leak_type for q in queries if q.is_leaky)
