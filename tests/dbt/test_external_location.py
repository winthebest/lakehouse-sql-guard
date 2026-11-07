"""Sources that dbt-duckdb reads from files (`external_location`).

dbt records the relation name of such a source as the table function call that reads it
(`read_parquet('data/events.parquet')`), and compiles every reference to it the same way. There
is no schema-qualified name to key on, so the call itself, normalised, identifies the source.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from sqlguard.cli import main
from sqlguard.dbt.check import check_model
from sqlguard.dbt.inline import InlineError, inline, relation_key
from sqlguard.dbt.manifest import parse_manifest
from sqlguard.dbt.spec import physical_table
from sqlguard.leakage.spec import SpecError
from tests.dbt.helpers import EVENTS, LABELS, SPINE, TRAINING, manifest, model, source

FIXTURE = Path(__file__).parent / "fixtures" / "external_location"
LABELS_CALL = "read_parquet('data/labels.parquet')"
EVENTS_CALL = "read_parquet('data/events/*.parquet', hive_partitioning=false)"


def external(name, call, leakage, cols):
    unique_id, node = source(name, leakage, cols, schema="lake")
    node["relation_name"] = call
    node["meta"]["external_location"] = call
    node["config"]["meta"]["external_location"] = call
    return unique_id, node


LABELS_ID, LABELS_NODE = external("labels", LABELS_CALL, SPINE, LABELS)
EVENTS_ID, EVENTS_NODE = external(
    "events", EVENTS_CALL, {"time_column": "event_ts", "availability_column": "ingested_at"}, EVENTS
)

CLEAN_SQL = (
    "select l.user_id, l.label_ts, l.churned, count(e.amount) as n "
    f"from {LABELS_CALL} as l left join {EVENTS_CALL} as e on e.user_id = l.user_id "
    "and e.event_ts < l.label_ts and e.ingested_at < l.label_ts group by 1, 2, 3"
)
LEAKY_SQL = (
    "select l.user_id, l.label_ts, l.churned, sum(e.amount) as total "
    f"from {LABELS_CALL} as l left join {EVENTS_CALL} as e on e.user_id = l.user_id "
    "group by 1, 2, 3"
)


def project(sql, deps=(LABELS_ID, EVENTS_ID)):
    return parse_manifest(
        manifest(
            (LABELS_ID, LABELS_NODE),
            (EVENTS_ID, EVENTS_NODE),
            model("train", sql, list(deps), TRAINING),
        )
    )


def check(sql):
    found = project(sql)
    return check_model(found, found.get("model.proj.train"))


def test_a_table_function_relation_has_a_key_and_two_spellings_of_it_share_it():
    key = relation_key(LABELS_CALL, "duckdb")
    assert key and key == relation_key("READ_PARQUET( 'data/labels.parquet' )", "duckdb")
    assert key != relation_key("read_parquet('data/other.parquet')", "duckdb")
    assert relation_key(EVENTS_CALL, "duckdb") != relation_key(
        "read_parquet('data/events/*.parquet', hive_partitioning=true)", "duckdb"
    )


def test_the_key_of_a_named_relation_is_unchanged():
    assert relation_key('"db"."raw"."Events"', "duckdb") == ("db", "raw", "events")
    assert relation_key("events", "duckdb") == ("events",)


def test_the_path_is_case_sensitive():
    assert relation_key("read_parquet('Data/A.parquet')", "duckdb") != relation_key(
        "read_parquet('data/a.parquet')", "duckdb"
    )


def test_external_sources_are_the_boundary_of_the_inlined_query():
    found = project(CLEAN_SQL)
    inlined = inline(found, found.get("model.proj.train"))
    assert set(inlined.boundaries) == {LABELS_ID, EVENTS_ID}
    assert "read_parquet" not in inlined.sql.lower()  # the query names the tables the spec does
    assert "FROM labels AS l\nLEFT JOIN events AS e" in inlined.sql


def test_the_physical_name_of_an_external_source_is_its_dbt_name():
    found = project(CLEAN_SQL)
    table = physical_table(found.get(EVENTS_ID), "duckdb", {})
    assert (table.name, table.target, table.relation) == ("events", "events", None)


def test_iceberg_table_meta_names_the_catalog_table_of_an_external_source():
    unique_id, node = external("events", EVENTS_CALL, {"time_column": "event_ts"}, EVENTS)
    node["meta"]["iceberg_table"] = "lake.events_v2"
    found = parse_manifest(manifest((unique_id, node)))
    table = physical_table(found.get(unique_id), "duckdb", {})
    assert (table.name, table.target) == ("events_v2", "lake.events_v2")
    assert table.relation == "events"  # the name the inlined query uses, mapped by `relations`


def test_a_spec_relation_can_rename_an_external_source():
    found = project(CLEAN_SQL)
    table = physical_table(found.get(EVENTS_ID), "duckdb", {"events": "lake.events_v2"})
    assert (table.name, table.target, table.relation) == ("events_v2", "lake.events_v2", "events")


def test_a_relation_name_that_names_nothing_is_a_spec_error_not_an_index_error(monkeypatch):
    unique_id, node = external("events", EVENTS_CALL, {"time_column": "event_ts"}, EVENTS)
    found = parse_manifest(manifest((unique_id, node)))
    monkeypatch.setattr("sqlguard.dbt.spec.relation_key", lambda *_: ())
    with pytest.raises(SpecError, match="names no table"):
        physical_table(found.get(unique_id), "duckdb", {})


def test_the_clean_query_over_external_sources_is_clean():
    result = check(CLEAN_SQL)
    assert result.report.verdict == "clean" and result.report.findings == []


def test_the_leaky_query_over_external_sources_is_leaky():
    result = check(LEAKY_SQL)
    assert result.report.verdict == "leaky"
    assert {f.rule_id for f in result.report.findings} == {"L3"}  # a lifetime sum, no upper bound


def test_compiled_spelling_may_differ_from_the_relation_name():
    respelled = (
        CLEAN_SQL.replace("read_parquet", "READ_PARQUET")
        .replace("('data/labels.parquet')", "( 'data/labels.parquet' )")
        .replace(", hive_partitioning=false", ",hive_partitioning = FALSE")
    )
    assert check(respelled).report.verdict == "clean"


def test_a_file_read_that_is_not_a_declared_source_is_never_taken_for_one():
    other = CLEAN_SQL.replace("data/events/*.parquet", "data/elsewhere/*.parquet")
    found = project(other)
    inlined = inline(found, found.get("model.proj.train"))
    assert set(inlined.boundaries) == {LABELS_ID}
    assert "data/elsewhere" in inlined.sql
    with pytest.raises(SpecError, match="no table that declares"):
        check_model(found, found.get("model.proj.train"))


def test_a_list_of_files_is_a_file_read_too():
    call = "read_parquet(['data/a.parquet', 'data/b.parquet'])"
    assert relation_key(call, "duckdb") == relation_key(
        "READ_PARQUET(['data/a.parquet',\n'data/b.parquet'])", "duckdb"
    )
    assert relation_key(call, "duckdb")


def test_two_sources_reading_the_same_file_are_an_error_not_a_pick():
    twin_id, twin = external("events_copy", EVENTS_CALL, {"time_column": "event_ts"}, EVENTS)
    found = parse_manifest(
        manifest(
            (LABELS_ID, LABELS_NODE),
            (EVENTS_ID, EVENTS_NODE),
            (twin_id, twin),
            model("train", CLEAN_SQL, [LABELS_ID, EVENTS_ID, twin_id], TRAINING),
        )
    )
    with pytest.raises(InlineError, match="events_copy.*events|events.*events_copy"):
        inline(found, found.get("model.proj.train"))


# --- a project dbt-duckdb compiles -----------------------------------------------------------


@pytest.fixture(scope="module")
def compiled(tmp_path_factory):
    pytest.importorskip("dbt.adapters.duckdb")
    root = tmp_path_factory.mktemp("external-lake")
    shutil.copytree(FIXTURE, root / "dbt")
    done = subprocess.run(
        [
            str(Path(sys.executable).with_name("dbt")),
            "compile",
            "--profiles-dir",
            ".",
            "--target-path",
            "build_x",
        ],
        cwd=root / "dbt",
        env={**os.environ, "DO_NOT_TRACK": "1", "DBT_SEND_ANONYMOUS_USAGE_STATS": "false"},
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    return root / "dbt" / "build_x" / "manifest.json"


def exit_code(argv):
    with pytest.raises(SystemExit) as info:
        main(argv)
    return info.value.code


def test_the_compiled_project_has_a_verdict_for_each_training_set(compiled, capsys):
    assert exit_code(["leakage", "dbt", "--manifest", str(compiled), "--json"]) == 1
    verdicts = {e["file"]: e["verdict"] for e in json.loads(capsys.readouterr().out)}
    assert verdicts == {"models/train_clean.sql": "clean", "models/train_leaky.sql": "leaky"}
