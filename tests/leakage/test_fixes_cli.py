"""`sqlguard leakage check --suggest-fix`: a diff for the user to read, JSON for tools, and the
file left alone."""

import json
from pathlib import Path

import pytest

from bench.leakage.world import WorldConfig, build_world
from sqlguard.cli import main
from sqlguard.core.catalog import LakehouseCatalog

DEMO = Path("examples/leakage/fix-demo/sql")
EXAMPLES = ["--spec", "examples/leakage/spec.yaml", "--schema", "examples/leakage/schema.yaml"]


def run(argv: list[str]) -> int:
    with pytest.raises(SystemExit) as exit_info:
        main(argv)
    return exit_info.value.code


def check(target, *extra) -> list[str]:
    return ["leakage", "check", str(target), *EXAMPLES, *extra]


def test_the_demo_prints_each_fix_and_a_unified_diff(capsys):
    code = run(check(DEMO, "--suggest-fix"))
    out = capsys.readouterr().out
    assert code == 1  # still leaky: suggesting a fix does not change the verdict
    assert "--- a/examples/leakage/fix-demo/sql/01_tickets_inner_join.sql" in out
    assert "+++ b/examples/leakage/fix-demo/sql/01_tickets_inner_join.sql (suggested)" in out
    assert "-  COUNT(*) AS tickets_total" in out
    assert "+  COUNT(t.customer_id) AS tickets_total" in out
    assert "+ASOF LEFT JOIN support_tickets AS t" in out
    assert "caveat: rows kept: INNER JOIN became LEFT JOIN" in out
    assert (
        "fix L3  [unverified; semantics unknown] orders" in out
    )  # explained only (entity-only CTE)
    assert "fix L6  [unverified; semantics unknown]" in out
    assert "[static_only; semantics changed] support_tickets AS t" in out
    assert "[static_only; semantics preserved] orders AS o" in out


def test_without_the_flag_nothing_about_fixes_is_printed(capsys):
    run(check(DEMO))
    out = capsys.readouterr().out
    assert "fix L" not in out and "suggested" not in out


def test_the_files_are_never_modified(capsys):
    before = {p: p.read_text() for p in DEMO.glob("*.sql")}
    run(check(DEMO, "--suggest-fix"))
    capsys.readouterr()
    assert {p: p.read_text() for p in DEMO.glob("*.sql")} == before


def test_json_carries_the_fixes(capsys):
    run(check(DEMO, "--suggest-fix", "--json"))
    results = {Path(r["file"]).name: r for r in json.loads(capsys.readouterr().out)}
    fix = results["01_tickets_inner_join.sql"]["fixes"][0]
    assert fix["rule"] == "L3" and fix["verification"] == "static_only"
    assert fix["semantics"] == "changed"  # INNER -> LEFT, COUNT(*) -> COUNT(key)
    assert "LEFT JOIN support_tickets" in fix["after_sql"] and "JOIN" in fix["before_sql"]
    assert fix["caveats"]
    explained = results["06_spend_by_customer_cte.sql"]["fixes"][0]
    assert explained["after_sql"] is None and explained["verification"] == "unverified"
    assert explained["semantics"] == "unknown"
    assert results["02_orders_event_time.sql"]["fixes"][0]["rule"] == "L4"


def test_json_without_the_flag_has_no_fixes_key(capsys):
    run(check(DEMO, "--json"))
    assert all("fixes" not in r for r in json.loads(capsys.readouterr().out))


def test_clean_queries_get_no_fixes(capsys):
    run(check("examples/leakage/sql/orders_90d.sql", "--suggest-fix", "--json"))
    (result,) = json.loads(capsys.readouterr().out)
    assert result["fixes"] == []


def test_with_a_warehouse_and_replay_the_fix_is_replayed(tmp_path, capsys):
    warehouse = tmp_path / "warehouse"
    build_world(LakehouseCatalog.local(warehouse), WorldConfig(seed=42))
    queries = tmp_path / "queries"
    queries.mkdir()
    (queries / "late.sql").write_text(
        "SELECT l.user_id, l.label_ts, l.churned, SUM(e.amount) AS amt FROM labels l "
        "LEFT JOIN events e ON e.user_id = l.user_id AND e.event_ts < l.label_ts GROUP BY 1, 2, 3"
    )
    code = run(
        [
            "leakage",
            "check",
            str(queries),
            "--spec",
            "bench/leakage/spec.yaml",
            "--warehouse",
            str(warehouse),
            "--replay",
            "--suggest-fix",
            "--json",
        ]
    )
    (result,) = json.loads(capsys.readouterr().out)
    assert code == 1 and result["verdict"] == "leaky"
    assert [f["verification"] for f in result["fixes"]] == ["static_and_replay"]
    assert [f["semantics"] for f in result["fixes"]] == ["preserved"]
