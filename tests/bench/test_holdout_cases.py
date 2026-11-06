"""A held-out split must not contain copies of the splits the rules were developed on, and an
author's output can be run through the same check before it is accepted."""

from pathlib import Path

import pytest
import yaml

from bench.leakage import duplicates
from bench.leakage.cases import TEMPLATES_DIR, Template, load_template, load_templates
from bench.leakage.splits import load_splits, seen_splits

RELATIONS = {
    "analytics.staging.stg_user_stats": "user_stats",
    "analytics.marts.fct_training_spine": "labels",
}
SOURCE = TEMPLATES_DIR / "dev" / "l2_latest_cte.sql"


def seen_templates() -> dict[str, list[Template]]:
    return {name: load_templates(name) for name in sorted(seen_splits())}


def candidate_from(tmp_path: Path, name: str, text: str) -> Path:
    directory = tmp_path / "candidate"
    directory.mkdir(exist_ok=True)
    (directory / f"{name}.sql").write_text(text, encoding="utf-8")
    return directory


def check(tmp_path: Path, name: str, text: str, **kwargs):
    directory = candidate_from(tmp_path, name, text)
    return duplicates.find_duplicates(
        duplicates.load_directory(directory), seen_templates(), relations=RELATIONS, **kwargs
    )


def test_every_held_out_template_differs_from_the_seen_splits():
    """Vacuous until the authors' templates are committed; then it is the acceptance gate."""
    held_out = [s for s in load_splits() if s not in seen_splits()]
    for split in held_out:
        found = duplicates.find_duplicates(
            load_templates(split), seen_templates(), relations=_relations(split)
        )
        assert not found, [str(d) for d in found]


def _relations(split: str) -> dict[str, str]:
    spec = yaml.safe_load(load_splits()[split].spec.read_text(encoding="utf-8"))
    return spec.get("relations") or {}


def test_holdout_v2_is_a_reference_for_the_next_held_out_set(tmp_path, capsys):
    """A split read and fixed against is in-sample: a new author's copy of one of its templates is
    caught by default, and a split named as the candidate is not its own reference."""
    source = TEMPLATES_DIR / "holdout_v2" / "hv2_clean_asof_stats_strict.sql"
    copy = candidate_from(tmp_path, "my_template", source.read_text(encoding="utf-8"))
    assert duplicates.main(["--candidate-dir", str(copy)]) == 1
    assert "holdout_v2/hv2_clean_asof_stats_strict" in capsys.readouterr().out

    duplicates.main(["--candidate", "holdout_v2"])
    out = capsys.readouterr().out
    assert "holdout_v2/" not in out and "dev, dev_v1b" in out


def test_holdout_v3_uses_the_spec_of_holdout_v2():
    manifest = load_splits()
    assert len(load_templates("holdout_v3")) == 146
    v2, v3 = (
        yaml.safe_load(manifest[name].spec.read_text(encoding="utf-8"))
        for name in ("holdout_v2", "holdout_v3")
    )
    assert v2 == v3


def test_independently_written_splits_share_only_three_known_shapes():
    """The two seen splits were written separately. With constants, `INTERVAL` spellings and
    wrappers ignored, three of v1's held-out templates are still copies of dev shapes (a lagged sum
    with 10 days for 8, a matured target encoding with 14 days for 1, a FILTER over one window); the
    other 43 are not. Sets the check flags here are returned for a person to read."""
    dev, v1b = load_templates("dev"), load_templates("dev_v1b")
    hits = duplicates.find_duplicates(v1b, {"dev": dev})
    assert set(duplicates.one_per_template(hits)) == {
        "hc07_target_encoding_matured",
        "hc11_filter_two_windows",
        "hc13_safe_event_lag",
    }
    back = duplicates.find_duplicates(dev, {"dev_v1b": v1b})
    assert set(duplicates.one_per_template(back)) == {
        "c03_window_agg_events",
        "c17_event_lag_over_delay",
        "l1_latest_prediction_only",
        "l4_event_lag_short",
        "l6_unmatured_target_encoding",
    }
    assert {d.kind for d in hits} == {"ast", "similar"}


def test_a_split_compared_with_itself_is_all_duplicates():
    templates = load_templates("dev_v1b")
    found = duplicates.find_duplicates(templates, {"dev_v1b": templates})
    assert {d.candidate.rsplit("-", 1)[0] for d in found} >= {t.template_id for t in templates}


def test_the_same_template_id_is_caught(tmp_path):
    found = check(tmp_path, "l2_latest_cte", "-- leak: L2\nSELECT 1")
    assert [d.kind for d in found if d.kind == "id"] == ["id"]


def test_whitespace_case_and_comments_do_not_hide_a_copy(tmp_path):
    body = SOURCE.read_text(encoding="utf-8")
    shouted = body.replace("SELECT", "select").replace("FROM", "from").replace("\n  ", "\n\t")
    lines = shouted.splitlines()
    headers = [line for line in lines if line.startswith("-- ")]
    body = [line for line in lines if not line.startswith("-- ")]
    commented = "\n".join([*headers, "-- reworded by someone else", *body])
    found = check(tmp_path, "my_template", commented)
    assert {"sql", "ast"} <= {d.kind for d in found}


def test_renamed_aliases_and_changed_literals_do_not_hide_a_copy(tmp_path):
    planted = """-- leak: L2
-- param col: cur.total_spend | cur.n_orders
-- param kind: JOIN | LEFT JOIN
WITH newest AS (
  SELECT user_id, total_spend, n_orders FROM user_stats
  QUALIFY row_number() OVER (PARTITION BY user_id ORDER BY updated_at DESC) = 1
)
SELECT s.user_id, s.label_ts, s.churned, {col} AS spend_feature
FROM labels s
{kind} newest cur ON cur.user_id = s.user_id
"""
    found = check(tmp_path, "renamed_copy", planted)
    assert found and {d.kind for d in found} == {"ast"}  # not the same text, the same query
    assert all(d.reference_split == "dev" for d in found)


def test_qualified_names_are_resolved_before_comparing(tmp_path):
    planted = """-- leak: L2
-- param col: lt.total_spend | lt.n_orders | lt.total_spend / NULLIF(lt.n_orders, 0)
-- param join: JOIN | LEFT JOIN
WITH latest AS (
  SELECT user_id, total_spend, n_orders FROM analytics.staging.stg_user_stats
  QUALIFY row_number() OVER (PARTITION BY user_id ORDER BY updated_at DESC) = 1
)
SELECT l.user_id, l.label_ts, l.churned, {col} AS feat
FROM analytics.marts.fct_training_spine l
{join} latest lt ON lt.user_id = l.user_id
"""
    found = check(tmp_path, "dbt_dressed_copy", planted)
    assert "ast" in {d.kind for d in found}
    # without the relation map the qualified names are other tables: nothing matches
    directory = candidate_from(tmp_path, "dbt_dressed_copy", planted)
    unmapped = duplicates.find_duplicates(duplicates.load_directory(directory), seen_templates())
    assert "ast" not in {d.kind for d in unmapped}


def test_a_different_query_is_not_flagged(tmp_path):
    different = """-- leak: clean
SELECT l.user_id, l.label_ts, l.churned,
       (SELECT MAX(s.n_orders) - MIN(s.n_orders) FROM user_stats s
         WHERE s.user_id = l.user_id AND s.updated_at < l.label_ts) AS order_range
FROM labels l
"""
    assert check(tmp_path, "fresh_idea", different) == []


def test_structure_masks_aliases_and_literals_only():
    a = "SELECT x.user_id, x.n + 7 AS big FROM events AS x WHERE x.amount > 10"
    b = "select e.user_id, e.n + 99 as huge from events e where e.amount > 3"
    c = "SELECT x.user_id, x.n + 7 AS big FROM user_stats AS x WHERE x.amount > 10"
    d = "SELECT x.user_id, x.n + 7 AS big FROM events AS x WHERE x.amount < 10"
    assert duplicates.ast_hash(a) == duplicates.ast_hash(b)
    assert duplicates.ast_hash(a) != duplicates.ast_hash(c)  # another table
    assert duplicates.ast_hash(a) != duplicates.ast_hash(d)  # another predicate


def test_unparseable_sql_is_an_error_not_a_pass():
    with pytest.raises(ValueError, match="cannot parse"):
        duplicates.ast_hash("SELEC FROM WHERE (")


def template(template_id: str, leak: str, sql: str) -> Template:
    return Template(template_id, "candidate", leak, {}, sql)


def test_shape_counts_report_what_is_short_of_the_quotas():
    plain = "SELECT l.user_id FROM labels l"
    qualified = "SELECT l.user_id FROM marts.fct_training_spine l"
    chained = "WITH a AS (SELECT 1 x), b AS (SELECT 2 y), c AS (SELECT 3 z) SELECT * FROM a"
    scd2 = "SELECT user_id FROM plan_history"
    templates = [template(f"c{i}", "clean", plain) for i in range(duplicates.MIN_CLEAN)]
    for leak in ("L1", "L2", "L3", "L4", "L5", "L6", "L7"):
        templates += [template(f"{leak}-{i}", leak, plain) for i in range(8)]
    full = duplicates.shape_counts(templates)
    assert full["dbt_share"] == 0.0 and full["short_of_quota"] == ["dbt-style share: 0.00 < 0.30"]

    dbt = [template(f"d{i}", "L1", s) for i, s in enumerate([qualified, chained, scd2] * 30)]
    ok = duplicates.shape_counts(templates + dbt)
    assert ok["short_of_quota"] == [] and ok["dbt_share"] >= 0.30

    thin = duplicates.shape_counts(templates[:10] + dbt)
    assert any(line.startswith("clean templates") for line in thin["short_of_quota"])
    assert any(line.startswith("L7 templates") for line in thin["short_of_quota"])


def test_the_command_line_check_reads_an_authors_directory(tmp_path, capsys):
    copy = candidate_from(tmp_path, "copy_of_dev", SOURCE.read_text(encoding="utf-8"))
    assert duplicates.main(["--candidate-dir", str(copy)]) == 1
    assert "near-duplicate" in capsys.readouterr().out

    fresh = tmp_path / "fresh"
    fresh.mkdir()
    (fresh / "fresh_idea.sql").write_text(
        "-- leak: clean\nSELECT l.user_id, l.label_ts, l.churned FROM labels l", encoding="utf-8"
    )
    assert duplicates.main(["--candidate-dir", str(fresh)]) == 0
    empty = tmp_path / "empty"
    empty.mkdir()
    assert duplicates.main(["--candidate-dir", str(empty)]) == 2


def test_loading_a_template_directory_uses_the_repository_format():
    loaded = duplicates.load_directory(TEMPLATES_DIR / "dev")
    assert len(loaded) == len(load_templates("dev"))
    assert loaded[0] == load_template(sorted((TEMPLATES_DIR / "dev").glob("*.sql"))[0], "candidate")


# --- single-edit variants of one shape -----------------------------------------------------------

BASE = """SELECT l.user_id, l.label_ts, l.churned, SUM(e.amount) AS amount_7d
FROM labels l
LEFT JOIN events e ON e.user_id = l.user_id
  AND e.event_ts < l.label_ts
  AND e.event_ts >= l.label_ts - INTERVAL 7 DAY
GROUP BY l.user_id, l.label_ts, l.churned"""
CLOSED = BASE.replace("e.event_ts < l.label_ts", "e.event_ts <= l.label_ts")

VARIANTS = {
    "predicate order": BASE.replace(
        "AND e.event_ts < l.label_ts\n  AND e.event_ts >= l.label_ts - INTERVAL 7 DAY",
        "AND e.event_ts >= l.label_ts - INTERVAL 7 DAY\n  AND e.event_ts < l.label_ts",
    ),
    "flipped comparison": BASE.replace("e.event_ts < l.label_ts\n", "l.label_ts > e.event_ts\n"),
    "select order": BASE.replace(
        "SELECT l.user_id, l.label_ts, l.churned, SUM(e.amount) AS amount_7d",
        "SELECT SUM(e.amount) AS amount_7d, l.churned, l.label_ts, l.user_id",
    ),
    "interval string": BASE.replace("INTERVAL 7 DAY", "INTERVAL '7 days'"),
    "to_days": BASE.replace("INTERVAL 7 DAY", "to_days(7)"),
    "changed constant": BASE.replace("INTERVAL 7 DAY", "INTERVAL 30 DAY"),
    "group by all": BASE.replace("GROUP BY l.user_id, l.label_ts, l.churned", "GROUP BY ALL"),
    "group by ordinals": BASE.replace(
        "GROUP BY l.user_id, l.label_ts, l.churned", "GROUP BY 1, 2, 3"
    ),
    "right join, tables swapped": """SELECT l.user_id, l.label_ts, l.churned,
               SUM(e.amount) AS amount_7d
        FROM events e RIGHT JOIN labels l ON e.user_id = l.user_id
          AND e.event_ts < l.label_ts AND e.event_ts >= l.label_ts - INTERVAL 7 DAY
        GROUP BY l.user_id, l.label_ts, l.churned""",
    "pass-through cte": BASE.replace("events e", "ev e").replace(
        "SELECT l.", "WITH ev AS (SELECT * FROM events) SELECT l.", 1
    ),
    "pass-through subquery": BASE.replace(
        "LEFT JOIN events e", "LEFT JOIN (SELECT * FROM events) e"
    ),
    "wrapped in select star": f"SELECT * FROM ({BASE}) w",
    "wrapped in a cte": f"WITH q AS ({BASE}) SELECT * FROM q",
    "NOT of the opposite comparison": BASE.replace(
        "e.event_ts < l.label_ts\n", "NOT (e.event_ts >= l.label_ts)\n"
    ),
    "interval moved to the other side": BASE.replace(
        "e.event_ts >= l.label_ts - INTERVAL 7 DAY", "e.event_ts + INTERVAL 7 DAY >= l.label_ts"
    ),
    "staging models that only project columns": """WITH stg_events AS (
          SELECT event_id, user_id, event_ts, amount FROM events
        ), spine AS (SELECT user_id, label_ts, churned FROM labels)
        SELECT s.user_id, s.label_ts, s.churned, SUM(e.amount) AS amount_7d
        FROM spine s LEFT JOIN stg_events e ON e.user_id = s.user_id
          AND e.event_ts < s.label_ts AND e.event_ts >= s.label_ts - INTERVAL 7 DAY
        GROUP BY ALL""",
    "staging model that renames columns": """WITH stg_events AS (
          SELECT user_id AS uid, event_ts AS happened_at, amount AS spend FROM events
        )
        SELECT l.user_id, l.label_ts, l.churned, SUM(e.spend) AS amount_7d
        FROM labels l LEFT JOIN stg_events e ON e.uid = l.user_id
          AND e.happened_at < l.label_ts AND e.happened_at >= l.label_ts - INTERVAL 7 DAY
        GROUP BY l.user_id, l.label_ts, l.churned""",
    "final cte that is only re-selected": f"""WITH final AS ({BASE})
        SELECT user_id, label_ts, churned, amount_7d FROM final""",
    "extra feature column": BASE.replace(
        "SUM(e.amount) AS amount_7d", "SUM(e.amount) AS amount_7d, COUNT(e.event_id) AS n_7d"
    ),
    "cast wrapper": BASE.replace("SUM(e.amount)", "SUM(CAST(e.amount AS DOUBLE))"),
    "coalesce wrapper": BASE.replace("SUM(e.amount)", "COALESCE(SUM(e.amount), 0)"),
    "inner join for left join": BASE.replace("LEFT JOIN events e", "JOIN events e"),
    "renamed and qualified": BASE.replace("labels l", "analytics.marts.fct_training_spine sp")
    .replace("l.", "sp.")
    .replace("events e", "analytics.staging.stg_events ev")
    .replace("e.", "ev."),
}
UNCAUGHT = {
    "a date cast on a time bound": BASE.replace(
        "e.event_ts < l.label_ts\n", "CAST(e.event_ts AS DATE) < CAST(l.label_ts AS DATE)\n"
    ),
    "a dead predicate": BASE.replace("AND e.event_ts <", "AND 1 = 1 AND e.event_ts <"),
    "join predicates moved to WHERE": """SELECT l.user_id, l.label_ts, l.churned,
               SUM(e.amount) AS amount_7d
        FROM labels l JOIN events e ON e.user_id = l.user_id
        WHERE e.event_ts < l.label_ts AND e.event_ts >= l.label_ts - INTERVAL 7 DAY
        GROUP BY l.user_id, l.label_ts, l.churned""",
    "date_diff for the window": BASE.replace(
        "e.event_ts >= l.label_ts - INTERVAL 7 DAY", "date_diff('day', e.event_ts, l.label_ts) <= 7"
    ),
    "FILTER (WHERE ...) for the window": """SELECT l.user_id, l.label_ts, l.churned,
               SUM(e.amount) FILTER (WHERE e.event_ts >= l.label_ts - INTERVAL 7 DAY) AS amount_7d
        FROM labels l LEFT JOIN events e ON e.user_id = l.user_id AND e.event_ts < l.label_ts
        GROUP BY l.user_id, l.label_ts, l.churned""",
    "a correlated scalar subquery": """SELECT l.user_id, l.label_ts, l.churned, (
          SELECT SUM(e.amount) FROM events e WHERE e.user_id = l.user_id
            AND e.event_ts < l.label_ts AND e.event_ts >= l.label_ts - INTERVAL 7 DAY
        ) AS amount_7d FROM labels l""",
    "a LATERAL join": """SELECT l.user_id, l.label_ts, l.churned, x.amount_7d
        FROM labels l, LATERAL (
          SELECT SUM(e.amount) AS amount_7d FROM events e WHERE e.user_id = l.user_id
            AND e.event_ts < l.label_ts AND e.event_ts >= l.label_ts - INTERVAL 7 DAY) x""",
    "an aggregate CTE joined back": """WITH f AS (
          SELECT l.user_id, l.label_ts, SUM(e.amount) AS amount_7d
          FROM labels l JOIN events e ON e.user_id = l.user_id AND e.event_ts < l.label_ts
            AND e.event_ts >= l.label_ts - INTERVAL 7 DAY GROUP BY l.user_id, l.label_ts)
        SELECT l.user_id, l.label_ts, l.churned, f.amount_7d FROM labels l
        LEFT JOIN f ON f.user_id = l.user_id AND f.label_ts = l.label_ts""",
}


def as_template(name: str, sql: str, label: str = "L4") -> Template:
    return Template(name, "candidate", label, {}, sql)


@pytest.mark.parametrize("name", VARIANTS)
def test_a_single_edit_of_a_reference_shape_is_flagged(name):
    reference = {"dev": [as_template("ref", BASE)]}
    found = duplicates.find_duplicates(
        [as_template("copy", VARIANTS[name])],
        reference,
        relations=RELATIONS
        | {
            "analytics.marts.fct_training_spine": "labels",
            "analytics.staging.stg_events": "events",
        },
    )
    assert found, name
    assert {d.kind for d in found} <= {"ast", "similar"}


def test_between_is_the_same_as_its_two_comparisons():
    closed_between = CLOSED.replace(
        "AND e.event_ts <= l.label_ts\n  AND e.event_ts >= l.label_ts - INTERVAL 7 DAY",
        "AND e.event_ts BETWEEN l.label_ts - INTERVAL 7 DAY AND l.label_ts",
    )
    assert duplicates.ast_hash(CLOSED) == duplicates.ast_hash(closed_between)
    # the strict bound is another query: BETWEEN is inclusive
    assert duplicates.ast_hash(BASE) != duplicates.ast_hash(closed_between)


def test_what_the_check_still_misses_is_listed_not_hidden():
    """These rewrites change the query's parts enough to fall below the similarity bar. They are
    the documented blind spots; if one becomes caught, move it to VARIANTS."""
    reference = {"dev": [as_template("ref", BASE)]}
    for name, sql in UNCAUGHT.items():
        assert not duplicates.find_duplicates([as_template("copy", sql)], reference), name


def test_a_date_cast_is_part_of_the_shape_but_a_numeric_cast_is_not():
    day = "SELECT CAST(ts AS DATE) AS d FROM events"
    assert duplicates.ast_hash(day) != duplicates.ast_hash("SELECT ts AS d FROM events")
    cast = "SELECT CAST(amount AS DOUBLE) AS d FROM events"
    assert duplicates.ast_hash(cast) == duplicates.ast_hash("SELECT amount AS d FROM events")


# --- the candidate set against itself -------------------------------------------------------


def test_padded_near_copies_do_not_count_toward_the_quotas():
    templates = [
        as_template("one", BASE, "clean"),
        as_template("two", VARIANTS["predicate order"], "clean"),
        as_template("three", VARIANTS["select order"], "clean"),
        as_template("four", VARIANTS["extra feature column"], "clean"),
        as_template("five", VARIANTS["to_days"], "clean"),
    ]
    pairs = duplicates.find_internal(templates)
    named = {a for a, _, _, _ in pairs} | {b for _, b, _, _ in pairs}
    assert named == {t.template_id for t in templates}
    [kept] = duplicates.distinct_templates(templates)
    assert kept.template_id == "five"  # the first by id of five near-copies
    counts = duplicates.shape_counts([kept])
    assert counts["templates"] == 1 and counts["per_type"]["clean"] == 1


def test_a_clean_twin_of_a_leaky_shape_is_kept_apart():
    leaky = as_template("leaky_twin", BASE, "L5")
    clean = as_template("clean_twin", CLOSED.replace("<=", "<"), "clean")
    assert duplicates.ast_hash(BASE) == duplicates.ast_hash(clean.body)
    assert len(duplicates.distinct_templates([leaky, clean])) == 2


def test_the_command_reports_collapsed_templates(tmp_path, capsys):
    directory = tmp_path / "padded"
    directory.mkdir()
    for name, sql in (("one", BASE), ("two", VARIANTS["group by all"])):
        (directory / f"{name}.sql").write_text(f"-- leak: clean\n{sql}\n", encoding="utf-8")
    duplicates.main(["--candidate-dir", str(directory), "--counts"])
    out = capsys.readouterr().out
    assert "near-copies of each other" in out and "collapsed" in out
    assert "distinct templates" in out
