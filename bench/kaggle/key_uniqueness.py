"""Count the (entity keys, prediction time) pairs that name more than one row of a label table.

    uv run python -m bench.kaggle.key_uniqueness [--out PATH]

The output goes to results/leakage/<date>-<sha>-key-uniqueness-dev.json unless --out says
otherwise. The checker takes a pair of entity keys and prediction time to name one row of the label
table, as `key_columns` does, and a window over the labels of earlier rows relies on it: two rows
of one pair let `LAG(label)` read a sibling stamped at the same instant. This script checks that
premise on every development data set that has real rows: the label table of each of the 14 Kaggle
cases (rebuilt from its generator) and the label table of the synthetic churn world of the leakage
benchmark. For each table with duplicated pairs it lists examples, and for the Kaggle cases it lists
the queries that have a window the checker accepted as history of earlier labels (those are the
queries the premise matters for). Nothing is changed: a duplicated pair is reported, not turned
into a rule. The generators were written by Claude models; no human authored any of the data.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import yaml

from bench.kaggle.evaluate import (
    CASES_DIR,
    ROOT,
    BuildError,
    convert_schema,
    generate_data,
    read_tables,
)
from sqlguard.leakage.analysis import analyze
from sqlguard.leakage.spec import LeakageSpec

SYNTHETIC = ROOT / ".warehouse" / "bench"


def duplicated_pairs(table, keys: list[str], time: str) -> dict:
    """`{rows, pairs, duplicated_pairs, extra_rows, examples}` for an Arrow table."""
    con = duckdb.connect()
    con.register("t", table)
    cols = ", ".join(f'"{c}"' for c in [*keys, time])
    found = con.sql(
        f"SELECT {cols}, COUNT(*) AS n FROM t GROUP BY {cols} HAVING COUNT(*) > 1 ORDER BY n DESC"
    ).fetchall()
    (pairs,) = con.sql(f"SELECT COUNT(*) FROM (SELECT DISTINCT {cols} FROM t)").fetchall()[0]
    return {
        "rows": table.num_rows,
        "pairs": pairs,
        "duplicated_pairs": len(found),
        "extra_rows": sum(r[-1] - 1 for r in found),
        "examples": [list(map(str, r)) for r in found[:3]],
    }


def accepted_windows(case_dir: Path, spec: dict, schema: dict) -> dict[str, int]:
    """Per query of the case, how many columns the checker read as the labels of earlier rows."""
    found = {}
    loaded = LeakageSpec.from_dict(spec)
    flat = convert_schema(schema)
    for name in sorted(p.name for p in case_dir.glob("*.sql")):
        try:
            analysis = analyze((case_dir / name).read_text(encoding="utf-8"), loaded, flat)
        except Exception as err:  # noqa: BLE001 - a query the analysis cannot read is recorded
            found[name] = -1
            print(f"  {case_dir.name}/{name}: {type(err).__name__}: {err}", file=sys.stderr)
            continue
        found[name] = len(analysis.past_label_args)
    return found


def kaggle_cases() -> dict:
    report = {}
    for case_dir in sorted(p for p in CASES_DIR.iterdir() if (p / "case.yaml").exists()):
        schema = yaml.safe_load((case_dir / "schema.yaml").read_text(encoding="utf-8"))
        spec = yaml.safe_load((case_dir / "spec.yaml").read_text(encoding="utf-8"))
        label = schema["spine"]["table"]
        with tempfile.TemporaryDirectory(prefix="sqlguard-keys-") as tmp:
            try:
                generate_data(case_dir, Path(tmp))
                table = read_tables(schema, Path(tmp))[label]
            except BuildError as err:
                report[case_dir.name] = {"table": label, "error": str(err)}
                continue
        entry = {
            "table": label,
            **duplicated_pairs(table, spec["entity_keys"], spec["prediction_ts"]),
        }
        windows = accepted_windows(case_dir, spec, schema)
        entry["queries_with_accepted_past_label_windows"] = sorted(
            q for q, n in windows.items() if n > 0
        )
        report[case_dir.name] = entry
    return report


def synthetic_label_table() -> dict:
    from bench.leakage.world import synthetic_world

    catalog = synthetic_world(SYNTHETIC, 42)
    spec = yaml.safe_load((ROOT / "bench" / "leakage" / "spec.yaml").read_text(encoding="utf-8"))
    table = catalog.table(spec["label_table"]).scan().to_arrow()
    return {
        "table": spec["label_table"],
        **duplicated_pairs(table, spec["entity_keys"], spec["prediction_ts"]),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sha = subprocess.run(
        ["git", "rev-parse", "--short=7", "HEAD"], cwd=ROOT, capture_output=True, text=True
    ).stdout.strip()
    stem = f"{datetime.now(UTC):%Y%m%d}-{sha}-key-uniqueness-dev.json"
    parser.add_argument("--out", type=Path, default=ROOT / "results" / "leakage" / stem)
    args = parser.parse_args(argv)
    cases = kaggle_cases()
    result = {
        "dev": True,
        "git_sha": sha,
        "premise": "(entity_keys, prediction_ts) names one row of the label table",
        "kaggle_cases": cases,
        "synthetic_world_w1": synthetic_label_table(),
        "synthetic_world_w2": "not available at this commit (made by the held-out preparation)",
        "cases_with_duplicated_pairs": {
            name: entry["duplicated_pairs"]
            for name, entry in cases.items()
            if entry.get("duplicated_pairs")
        },
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {args.out}")
    print(json.dumps({"cases_with_duplicated_pairs": result["cases_with_duplicated_pairs"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
