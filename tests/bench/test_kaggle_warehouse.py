"""Warehouses built by bench/kaggle/evaluate.py: commits in time order with logical times."""

from datetime import datetime
from pathlib import Path

import pytest
import yaml

from bench.kaggle import evaluate as ev
from sqlguard.core.catalog import LakehouseCatalog

ROOT = Path(__file__).resolve().parents[2]
CASE_IDS = yaml.safe_load((ROOT / "bench/kaggle/eval-prereg.yaml").read_text())["inputs"]["cases"]

SCHEMA = {
    "tables": {
        "people": {"columns": {"id": "VARCHAR", "group_1": "VARCHAR"}},
        "events": {
            "columns": {"id": "VARCHAR", "event_ts": "TIMESTAMP", "loaded_at": "TIMESTAMP"},
            "event_time": "event_ts",
            "availability_time": "loaded_at",
        },
        "spine": {
            "columns": {"id": "VARCHAR", "ts": "TIMESTAMP", "y": "TINYINT"},
            "event_time": "ts",
        },
    },
    "spine": {"table": "spine", "entity_keys": ["id"], "prediction_time": "ts"},
}
SPEC = {"static_tables": ["people"], "outcome_columns": []}


def write_csvs(directory: Path, **files: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (directory / f"{name}.csv").write_text(text)
    return directory


@pytest.fixture
def data(tmp_path):
    return write_csvs(
        tmp_path / "data",
        people="id,group_1\na,g1\nb,g1\n",
        events=(
            "id,event_ts,loaded_at\n"
            "a,2026-03-01 10:00:00,2026-03-02 00:00:00\n"
            "a,2026-03-01 11:00:00,2026-03-02 00:00:00\n"
            "b,2026-03-01 12:00:00,2026-03-04 00:00:00\n"
        ),
        spine="id,ts,y\na,2026-03-03 00:00:00,1\nb,2026-03-05 00:00:00,0\n",
    )


def visible(catalog: LakehouseCatalog, table: str) -> list[datetime]:
    return sorted(s.visible_at for s in catalog.snapshots(table))


def test_commits_follow_the_registered_rules(data, tmp_path):
    result = ev.build_warehouse(SCHEMA, SPEC, data, tmp_path / "wh")
    assert result.status == "built"
    assert result.rows == {"people": 2, "events": 3, "spine": 2}
    assert result.commits == {"people": 1, "events": 2, "spine": 1}  # events: two load times
    catalog = LakehouseCatalog.local(tmp_path / "wh")
    assert visible(catalog, "people") == [datetime(2026, 2, 28, 10)]  # a day before the first event
    assert visible(catalog, "events") == [datetime(2026, 3, 2), datetime(2026, 3, 4)]
    assert visible(catalog, "spine") == [datetime(2026, 3, 6, 0)]  # a day after the last event time
    assert catalog.read("spine").num_rows == 2


def test_replay_would_see_rows_only_after_their_load_time(data, tmp_path):
    ev.build_warehouse(SCHEMA, SPEC, data, tmp_path / "wh")
    catalog = LakehouseCatalog.local(tmp_path / "wh")

    def rows_at(moment: datetime) -> int:
        snapshot = catalog.snapshot_as_of("events", moment)
        return 0 if snapshot is None else catalog.read("events", snapshot).num_rows

    assert rows_at(datetime(2026, 3, 1, 23)) == 0
    assert rows_at(datetime(2026, 3, 3)) == 2
    assert rows_at(datetime(2026, 3, 4, 1)) == 3


def test_stored_types_follow_the_registered_mapping(data, tmp_path):
    ev.build_warehouse(SCHEMA, SPEC, data, tmp_path / "wh")
    catalog = LakehouseCatalog.local(tmp_path / "wh")
    assert catalog.schema_for(["spine"])["spine"] == {
        "id": "VARCHAR",
        "ts": "TIMESTAMP",
        "y": "INT",
    }


def test_event_time_orders_commits_when_there_is_no_load_time(tmp_path):
    schema = {
        "tables": {
            "log": {
                "columns": {"id": "VARCHAR", "ts": "TIMESTAMP"},
                "event_time": "ts",
            },
            "spine": {
                "columns": {"id": "VARCHAR", "ts": "TIMESTAMP"},
                "event_time": "ts",
            },
        },
        "spine": {"table": "spine"},
    }
    data = write_csvs(
        tmp_path / "data",
        log="id,ts\na,2026-03-02 00:00:00\na,2026-03-01 00:00:00\nb,2026-03-01 00:00:00\n",
        spine="id,ts\na,2026-03-03 00:00:00\n",
    )
    result = ev.build_warehouse(schema, {}, data, tmp_path / "wh")
    assert result.commits == {"log": 2, "spine": 1}
    catalog = LakehouseCatalog.local(tmp_path / "wh")
    assert visible(catalog, "log") == [datetime(2026, 3, 1), datetime(2026, 3, 2)]


def test_integer_time_uses_the_declared_unit(tmp_path):
    schema = {
        "tables": {
            "spine": {
                "columns": {"id": "VARCHAR", "t": "BIGINT"},
                "event_time": "t",
                "time_unit": "ms",
            },
            "log": {
                "columns": {"id": "VARCHAR", "t": "BIGINT"},
                "event_time": "t",
                "time_unit": "ms",
            },
        },
        "spine": {"table": "spine"},
    }
    data = write_csvs(
        tmp_path / "data", spine="id,t\na,5000\n", log="id,t\na,1000\na,2000\na,2000\n"
    )
    result = ev.build_warehouse(schema, {}, data, tmp_path / "wh")
    assert result.status == "built"
    catalog = LakehouseCatalog.local(tmp_path / "wh")
    assert visible(catalog, "log") == [
        datetime(1970, 1, 1, 0, 0, 1),
        datetime(1970, 1, 1, 0, 0, 2),
    ]
    assert catalog.schema_for(["log"])["log"]["t"] == "BIGINT"  # still an integer in the table


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (lambda s: s["tables"]["spine"]["columns"].update(y="JSON"), "unsupported"),
        (lambda s: s["tables"]["spine"].pop("event_time"), "no event_time"),
        (lambda s: s["tables"]["events"].update(availability_time="nope"), "not in the schema"),
    ],
)
def test_a_case_that_cannot_be_placed_in_time_is_static_only(data, tmp_path, change, reason):
    import copy

    schema = copy.deepcopy(SCHEMA)
    change(schema)
    result = ev.build_warehouse(schema, SPEC, data, tmp_path / "wh")
    assert result.status == "static_only"
    assert reason in result.reason
    assert not (tmp_path / "wh").exists()  # nothing was written


def test_integer_time_without_a_unit_is_static_only(tmp_path):
    schema = {
        "tables": {"spine": {"columns": {"id": "VARCHAR", "t": "BIGINT"}, "event_time": "t"}},
        "spine": {"table": "spine"},
    }
    data = write_csvs(tmp_path / "data", spine="id,t\na,5\n")
    result = ev.build_warehouse(schema, {}, data, tmp_path / "wh")
    assert result.status == "static_only" and "time_unit" in result.reason


def test_a_missing_csv_is_static_only(tmp_path):
    data = write_csvs(tmp_path / "data", people="id,group_1\na,g1\n")
    result = ev.build_warehouse(SCHEMA, SPEC, data, tmp_path / "wh")
    assert result.status == "static_only" and "did not write" in result.reason


def test_a_null_time_is_static_only(tmp_path, data):
    write_csvs(data, spine="id,ts,y\na,,1\n")
    result = ev.build_warehouse(SCHEMA, SPEC, data, tmp_path / "wh")
    assert result.status == "static_only" and "null" in result.reason


@pytest.mark.parametrize("case", CASE_IDS)
def test_every_registered_case_builds_a_warehouse(case, tmp_path):
    case_dir = ev.CASES_DIR / case
    schema = yaml.safe_load((case_dir / "schema.yaml").read_text())
    spec = yaml.safe_load((case_dir / "spec.yaml").read_text())
    out = tmp_path / "data"
    out.mkdir()
    ev.generate_data(case_dir, out)
    result = ev.build_warehouse(schema, spec, out, tmp_path / "wh")
    assert result.status == "built", result.reason
    catalog = LakehouseCatalog.local(tmp_path / "wh")
    assert catalog.table_names() == sorted(schema["tables"])
    label = schema["spine"]["table"]
    assert catalog.read(label).num_rows == result.rows[label]
    # the label table is committed after every event time it holds
    event_column = schema["tables"][label]["event_time"]
    assert len(catalog.snapshots(label)) == 1
    last = max(ev.column_times(label, schema["tables"][label], event_column, catalog.read(label)))
    assert visible(catalog, label)[0] > last
