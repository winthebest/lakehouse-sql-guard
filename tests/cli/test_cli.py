"""End-to-end CLI behaviour: exit codes are the contract CI hooks rely on."""

import json

import pytest
import yaml

from sqlguard.cli import main
from tests.leakage.conftest import SCHEMA, SPEC

CLEAN = (
    "SELECT l.user_id, l.label_ts, l.churned, s.total_spend FROM labels l "
    "ASOF JOIN user_stats s ON l.user_id = s.user_id AND s.updated_at < l.label_ts"
)
LEAKY = (
    "SELECT l.user_id, l.label_ts, l.churned, s.total_spend FROM labels l "
    "JOIN user_stats s ON s.user_id = l.user_id"
)


def run(argv: list[str]) -> int:
    with pytest.raises(SystemExit) as exit_info:
        main(argv)
    return exit_info.value.code


@pytest.fixture
def files(tmp_path):
    (tmp_path / "spec.yaml").write_text(yaml.safe_dump(SPEC))
    (tmp_path / "schema.yaml").write_text(yaml.safe_dump(SCHEMA))
    queries = tmp_path / "queries"
    queries.mkdir()
    (queries / "clean.sql").write_text(CLEAN)
    return tmp_path


def leakage(files, target, *extra):
    return [
        "leakage",
        "check",
        str(target),
        "--spec",
        str(files / "spec.yaml"),
        "--schema",
        str(files / "schema.yaml"),
        *extra,
    ]


def test_clean_query_exits_zero(files, capsys):
    assert run(leakage(files, files / "queries" / "clean.sql")) == 0
    assert capsys.readouterr().out.startswith("CLEAN")


def test_leaky_query_in_directory_exits_one_with_json(files, capsys):
    (files / "queries" / "leaky.sql").write_text(LEAKY)
    assert run(leakage(files, files / "queries", "--json")) == 1
    reports = {r["file"].rsplit("/", 1)[-1]: r for r in json.loads(capsys.readouterr().out)}
    assert reports["clean.sql"]["verdict"] == "clean"
    assert reports["leaky.sql"]["verdict"] == "leaky" and "L2" in reports["leaky.sql"]["leak_types"]


def test_usage_errors_exit_two(files, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert run(leakage(files, empty)) == 2
    assert (
        run(["leakage", "check", str(files / "queries"), "--spec", str(files / "spec.yaml")]) == 2
    )
    assert run(leakage(files, files / "queries", "--replay")) == 2
    assert run(leakage(files, files / "missing.sql")) == 2


def test_safety_decisions_map_to_exit_codes(capsys):
    scope = ["--scope", "db.orders"]
    assert run(["safety", "check", "SELECT * FROM db.orders", *scope]) == 0
    assert run(["safety", "check", "SELECT * FROM db.orders"]) == 1  # out of scope -> deny
    capsys.readouterr()
    assert run(["safety", "check", "DELETE FROM db.orders", *scope, "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["decision"] == "ask"


EXAMPLES = ["--spec", "examples/leakage/spec.yaml", "--schema", "examples/leakage/schema.yaml"]


def test_examples_have_expected_verdicts(capsys):
    sql = "examples/leakage/sql"
    assert run(["leakage", "check", f"{sql}/orders_90d.sql", *EXAMPLES]) == 0
    assert run(["leakage", "check", f"{sql}/tickets_total.sql", *EXAMPLES]) == 1
    # event-time-only bound: unverifiable without replay, fails closed by default
    assert run(["leakage", "check", f"{sql}/orders_event_time.sql", *EXAMPLES]) == 1
    event_time = f"{sql}/orders_event_time.sql"
    assert run(["leakage", "check", event_time, *EXAMPLES, "--allow-unverifiable"]) == 0
    # several targets at once, as pre-commit passes them
    assert (
        run(["leakage", "check", f"{sql}/orders_90d.sql", f"{sql}/tickets_total.sql", *EXAMPLES])
        == 1
    )
