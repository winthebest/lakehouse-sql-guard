"""A held-out split must not contain copies of the splits the rules were developed on, and an
author's output can be run through the same check before it is accepted."""

from pathlib import Path

import pytest

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
    import yaml

    spec = yaml.safe_load(load_splits()[split].spec.read_text(encoding="utf-8"))
    return spec.get("relations") or {}


def test_the_seen_splits_are_independent_of_each_other():
    """Sanity of the check itself: v1's held-out templates and the dev templates were written
    separately, and none is a copy of the other."""
    dev, v1b = load_templates("dev"), {"dev_v1b": load_templates("dev_v1b")}
    assert duplicates.find_duplicates(dev, v1b) == []
    assert duplicates.find_duplicates(load_templates("dev_v1b"), {"dev": dev}) == []


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
SELECT l.user_id, l.label_ts, l.churned, COUNT(e.event_id) AS n_events
FROM labels l
LEFT JOIN events e ON e.user_id = l.user_id AND e.ingested_at < l.label_ts
GROUP BY ALL
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
    templates = [template(f"c{i}", "clean", plain) for i in range(25)]
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
