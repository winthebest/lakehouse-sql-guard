"""What is registered before the held-out sets exist: the sample sizes the targets need, the
quotas an author's set is held to, and the files the freeze covers."""

import pytest

from bench.leakage import duplicates, freeze
from bench.leakage.cases import Template
from bench.leakage.metrics import clopper_pearson_one_sided


@pytest.mark.parametrize(
    ("kind", "n", "threshold", "allowed"),
    [
        ("fpr", 124, 0.05, 2),  # H1a: at most 2 of 124 clean templates flagged
        ("fpr", 124, 0.10, 6),  # H1b
        ("fpr", 30, 0.20, 2),  # H3
        ("fpr", 70, 0.05, 0),  # the old set size: only a perfect score proves 5%
        ("fpr", 20, 0.05, -1),  # too small to prove it at all
        ("recall", 70, 0.90, 2),  # H1a: at most 2 of 70 leaky templates missed
        ("recall", 70, 0.85, 5),  # H1b
        ("recall", 30, 0.75, 3),  # H3
        ("precision", 30, 0.30, 16),  # H2: 14 of 30 findings true is enough (16 not)
    ],
)
def test_the_most_errors_a_sample_size_allows(kind, n, threshold, allowed):
    assert freeze.max_errors_allowed(kind, n, threshold) == allowed


@pytest.mark.parametrize(("errors", "smallest"), [(0, 59), (1, 93), (2, 124), (3, 153)])
def test_the_smallest_sample_that_can_prove_a_five_percent_false_positive_rate(errors, smallest):
    assert freeze.min_sample_size("fpr", errors, 0.05) == smallest


def test_the_smallest_sample_for_a_lower_bound_target():
    # 2 misses allowed at 0.90 needs 70 leaky templates, 1 miss needs fewer
    assert freeze.min_sample_size("recall", 2, 0.90) <= 70
    one = freeze.min_sample_size("recall", 1, 0.90)
    two = freeze.min_sample_size("recall", 2, 0.90)
    assert one > 0 and two > one


def test_a_target_is_met_on_the_bound_not_on_the_point_estimate():
    # 3 flagged of 124 is 2.4% but the exact 95% upper bound is above 5%
    assert not freeze.target_met("fpr", 3, 124, 0.05)
    assert freeze.target_met("fpr", 2, 124, 0.05)
    assert clopper_pearson_one_sided(2, 124) <= 0.05 < clopper_pearson_one_sided(3, 124)
    # 3 missed of 70 is 95.7% recall, but the lower bound is under 0.90
    assert not freeze.target_met("recall", 3, 70, 0.90)
    assert freeze.target_met("recall", 2, 70, 0.90)
    # nothing measured proves nothing
    assert not freeze.target_met("fpr", 0, 0, 0.05)
    assert not freeze.target_met("recall", 0, 0, 0.5)


def test_describe_target_records_the_size_and_the_errors_it_allows():
    t = freeze.describe_target("fpr", 124, 0.05)
    assert t["bound"] == "upper" and t["level"] == 0.95
    assert t["n"] == 124 and t["max_errors"] == 2
    assert t["min_n_by_errors"] == {"0": 59, "1": 93, "2": 124, "3": 153}
    r = freeze.describe_target("recall", 70, 0.90)
    assert r["bound"] == "lower" and r["max_errors"] == 2
    assert r["min_n_by_errors"]["2"] <= 70


def test_unknown_kinds_and_levels_are_refused():
    with pytest.raises(ValueError):
        freeze.max_errors_allowed("accuracy", 10, 0.5)
    with pytest.raises(ValueError):
        freeze.max_errors_allowed("fpr", 10, 1.5)
    with pytest.raises(ValueError):
        freeze.max_errors_allowed("fpr", -1, 0.5)
    with pytest.raises(ValueError):
        freeze.min_sample_size("fpr", 5, 0.0)


# --- the quotas of the second held-out round, and the gate against developer fixtures ---------


def _t(template_id: str, leak: str, sql: str) -> Template:
    return Template(template_id, "candidate", leak, {}, sql)


def test_the_new_quotas_follow_from_the_sample_sizes_the_targets_need():
    v4 = duplicates.V4_QUOTAS
    assert v4.clean == 124 == freeze.min_sample_size("fpr", 2, 0.05)
    assert v4.leaky_per_type == 10 and 7 * v4.leaky_per_type == 70
    assert freeze.max_errors_allowed("recall", 7 * v4.leaky_per_type, 0.90) == 2
    assert (
        duplicates.Quotas(
            duplicates.MIN_CLEAN, duplicates.MIN_LEAKY_PER_TYPE, duplicates.MIN_DBT_SHARE
        )
        == duplicates.V3_QUOTAS
    )


def test_shape_counts_hold_a_set_to_the_quotas_it_is_given():
    plain = "SELECT l.user_id FROM labels l"
    qualified = "SELECT l.user_id FROM marts.fct_training_spine l"
    templates = [_t(f"c{i}", "clean", plain) for i in range(60)]
    for leak in ("L1", "L2", "L3", "L4", "L5", "L6", "L7"):
        templates += [_t(f"{leak}-{i}", leak, plain) for i in range(8)]
    templates += [_t(f"d{i}", "L1", qualified) for i in range(100)]
    assert duplicates.shape_counts(templates)["short_of_quota"] == []  # the old quotas
    short = duplicates.shape_counts(templates, duplicates.V4_QUOTAS)["short_of_quota"]
    assert "clean templates: 60 < 124" in short
    assert any(line.startswith("L7 templates: 8 < 10") for line in short)


def test_fixture_queries_are_read_from_python_test_files(tmp_path):
    source = tmp_path / "test_fixture_example.py"
    source.write_text(
        'SQL = """SELECT l.user_id, SUM(e.amount) AS s FROM labels l\n'
        'JOIN events e ON e.user_id = l.user_id AND {bound} GROUP BY 1"""\n'
        'OTHER = "not a query at all"\n'
        'TWICE = "SELECT 1 AS x FROM t"\n'
        'AGAIN = "SELECT 1 AS x FROM t"\n'
        'BROKEN = "SELECT FROM WHERE ("\n',
        encoding="utf-8",
    )
    found = duplicates.fixture_templates([tmp_path])
    assert len(found) == 2  # the placeholder is filled in, the duplicate and the junk dropped
    assert all(t.split == "fixtures" and "test_fixture_example.py" in t.template_id for t in found)
    assert any("SUM" in t.body for t in found)


def test_a_candidate_that_copies_a_developer_fixture_is_caught(tmp_path):
    source = tmp_path / "test_fixture_example.py"
    source.write_text(
        'Q = "SELECT l.user_id, l.label_ts, COUNT(*) AS n FROM labels l '
        'JOIN events e ON e.user_id = l.user_id AND e.event_ts < l.label_ts GROUP BY 1, 2"\n',
        encoding="utf-8",
    )
    copy = _t(
        "copy",
        "clean",
        "SELECT x.user_id, x.label_ts, COUNT(*) AS cnt FROM labels AS x "
        "JOIN events AS ev ON ev.user_id = x.user_id AND ev.event_ts < x.label_ts GROUP BY 1, 2",
    )
    fresh = _t("fresh", "clean", "SELECT user_id, label_ts, churned FROM labels")
    hits = duplicates.find_duplicates(
        [copy, fresh], {"fixtures": duplicates.fixture_templates([tmp_path])}
    )
    assert {h.candidate.rsplit("-", 1)[0] for h in hits} == {"copy"}
    assert hits[0].reference_split == "fixtures"


def test_the_command_line_takes_fixture_files_and_a_spec_for_the_relations(tmp_path, capsys):
    (tmp_path / "test_fx.py").write_text(
        'Q = "SELECT s.user_id, s.ts_ms FROM interactions s"\n', encoding="utf-8"
    )
    cand = tmp_path / "cand"
    cand.mkdir()
    (cand / "copy.sql").write_text(
        "-- leak: clean\nSELECT a.user_id, a.ts_ms FROM staging.stg_interactions AS a",
        encoding="utf-8",
    )
    spec = "bench/leakage/spec_holdout_v4_w2.yaml"
    code = duplicates.main(
        [
            "--candidate-dir",
            str(cand),
            "--against",
            "dev",
            "--against-tests",
            str(tmp_path),
            "--relations-of",
            spec,
        ]
    )
    out = capsys.readouterr().out
    assert code == 1 and "fixtures" in out
