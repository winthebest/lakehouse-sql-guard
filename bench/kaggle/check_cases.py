"""Self-check for the Kaggle leak cases in bench/kaggle/cases.

For every case directory it:

1. runs generate.py twice into temporary directories (the output must be identical)
   and loads each CSV with the types declared in schema.yaml;
2. checks that schema.yaml and spec.yaml describe the same tables and time columns,
   and that no SQL file contains a word that gives its label away;
3. runs every query and checks it returns exactly one row per spine row with the
   spine key, entity keys, prediction time and label columns;
4. checks that each leaky query's features differ from its clean twin on some rows;
5. replays sampled spine rows point in time: every table is cut down to the rows
   whose event time (and load time, where declared) is strictly before that row's
   prediction time (or at it, when the spec says ``boundary: inclusive``); the spine
   row itself stays, because its own attributes are the prediction's inputs. The
   label of the row itself, labels of other rows that are not final yet
   (``label_known_after``) and every declared outcome column are replaced by
   different values. A clean query must return the same features as on the full
   data for every sampled row; a leaky query must change for at least one.
   Rows where a pair differs make up half of the sample.

Run with: uv run python bench/kaggle/check_cases.py [case_id ...] [--rows N]
"""

from __future__ import annotations

import argparse
import math
import random
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import duckdb
import yaml

HERE = Path(__file__).resolve().parent
CASES = HERE / "cases"

NUMERIC = (
    "TINYINT",
    "SMALLINT",
    "INTEGER",
    "BIGINT",
    "HUGEINT",
    "DOUBLE",
    "FLOAT",
    "REAL",
    "DECIMAL",
    "UTINYINT",
    "USMALLINT",
    "UINTEGER",
    "UBIGINT",
)
INTEGER_TIME = ("TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT")
UNIT_SECONDS = {"s": 1, "ms": 1000}
DURATION_SECONDS = {"second": 1, "minute": 60, "hour": 3600, "day": 86400, "week": 604800}
FORBIDDEN_SQL_WORDS = re.compile(r"(?i)\b(leak\w*|clean|future|label)\b")


class CaseError(Exception):
    pass


def qi(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def parse_duration(text: str) -> int:
    m = re.fullmatch(r"\s*(\d+)\s*(second|minute|hour|day|week)s?\s*", str(text))
    if not m:
        raise CaseError(f"cannot parse duration {text!r}")
    return int(m.group(1)) * DURATION_SECONDS[m.group(2)]


def base_type(t: str) -> str:
    return t.split("(")[0].strip().upper()


def perturb_expr(col: str, typ: str) -> str:
    c, b = qi(col), base_type(typ)
    if b in NUMERIC:
        return f"CAST(CASE WHEN {c} IS NULL THEN 1 ELSE -({c}) - 7 END AS {typ})"
    if b == "BOOLEAN":
        return f"CASE WHEN {c} IS NULL THEN TRUE ELSE NOT {c} END"
    if b in ("DATE", "TIMESTAMP", "TIMESTAMPTZ"):
        return f"CASE WHEN {c} IS NULL THEN CAST('1999-01-01' AS {typ}) ELSE NULL END"
    return f"COALESCE(CAST({c} AS VARCHAR), '') || '~p'"


def time_shift(col_sql: str, typ: str, unit: str | None, seconds: int) -> str:
    """col + duration, in the column's own type."""
    if base_type(typ) in INTEGER_TIME:
        if unit not in UNIT_SECONDS:
            raise CaseError(f"integer time column needs time_unit s or ms, got {unit!r}")
        return f"({col_sql} + {seconds * UNIT_SECONDS[unit]})"
    return f"({col_sql} + INTERVAL {seconds} SECOND)"


def values_equal(a, b) -> bool:
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, float) or isinstance(b, float):
        try:
            fa, fb = float(a), float(b)
        except (TypeError, ValueError):
            return a == b
        if math.isnan(fa) and math.isnan(fb):
            return True
        return math.isclose(fa, fb, rel_tol=1e-9, abs_tol=1e-9)
    return a == b


class Case:
    def __init__(self, path: Path):
        self.path = path
        self.id = path.name
        self.meta = yaml.safe_load((path / "case.yaml").read_text())
        self.schema = yaml.safe_load((path / "schema.yaml").read_text())
        self.spec = yaml.safe_load((path / "spec.yaml").read_text())
        self.tables = self.schema["tables"]
        sp = self.schema["spine"]
        self.label_table = sp["table"]
        self.entity_keys = list(sp["entity_keys"])
        self.pred = sp["prediction_time"]
        self.labels = list(sp["label_columns"])
        chk = self.meta.get("check", {}) or {}
        self.spine_key = list(chk.get("spine_key") or self.entity_keys + [self.pred])
        self.spine_filter = chk.get("spine_filter")
        self.boundary_inclusive = self.spec.get("boundary", "strict") == "inclusive"
        lka = self.spec.get("label_known_after")
        self.lka = parse_duration(lka) if lka is not None else None
        self.outcome = {}
        for item in self.spec.get("outcome_columns", []) or []:
            t, c = item.split(".", 1)
            self.outcome.setdefault(t, set()).add(c)
        self.static = set(self.spec.get("static_tables", []) or [])
        self.queries = self.meta["queries"]

    # -- consistency -------------------------------------------------------
    def check_consistency(self) -> None:
        s = self.spec
        if s["label_table"] != self.label_table:
            raise CaseError("spec.label_table differs from schema spine.table")
        if list(s["entity_keys"]) != self.entity_keys:
            raise CaseError("spec.entity_keys differs from schema spine.entity_keys")
        if s["prediction_ts"] != self.pred:
            raise CaseError("spec.prediction_ts differs from schema spine.prediction_time")
        if list(s["label_columns"]) != self.labels:
            raise CaseError("spec.label_columns differs from schema spine.label_columns")
        st = s.get("time_columns", {}) or {}
        sa = s.get("availability_columns", {}) or {}
        for name, t in self.tables.items():
            cols = t["columns"]
            ev = t.get("event_time")
            av = t.get("availability_time")
            if name in self.static:
                if ev:
                    raise CaseError(f"{name}: static table with an event_time")
            elif name == self.label_table:
                if ev != self.pred:
                    raise CaseError(f"{name}: spine event_time must be the prediction time")
                if name in st and st[name] != ev:
                    raise CaseError(f"{name}: spec time column differs from schema")
            else:
                if not ev or st.get(name) != ev:
                    raise CaseError(f"{name}: event_time missing or differs from spec")
            if (av or None) != (sa.get(name) or None):
                raise CaseError(f"{name}: availability column differs from spec")
            for c in [ev, av]:
                if c and c not in cols:
                    raise CaseError(f"{name}: unknown time column {c}")
        for t, cs in self.outcome.items():
            for c in cs:
                if c not in self.tables.get(t, {}).get("columns", {}):
                    raise CaseError(f"outcome column {t}.{c} not in schema")
        for q in self.queries:
            sql = (self.path / q["file"]).read_text()
            if FORBIDDEN_SQL_WORDS.search(sql):
                raise CaseError(f"{q['file']}: SQL text contains a word that gives the label away")
            if q["label"] == "leaky" and not q.get("leak_type"):
                raise CaseError(f"{q['file']}: leaky query without leak_type")

    # -- loading ------------------------------------------------------------
    def load(self, con: duckdb.DuckDBPyConnection, data_dir: Path) -> None:
        con.execute("CREATE SCHEMA src")
        for name, t in self.tables.items():
            f = data_dir / f"{name}.csv"
            if not f.exists():
                raise CaseError(f"generator did not write {f.name}")
            header = f.open().readline().strip().split(",")
            cols = t["columns"]
            if header != list(cols):
                raise CaseError(f"{f.name}: header {header} does not match schema {list(cols)}")
            spec = "{" + ", ".join(f"'{c}': '{ty}'" for c, ty in cols.items()) + "}"
            con.execute(
                f"CREATE TABLE src.{qi(name)} AS SELECT * FROM read_csv('{f}', header=true, "
                f"columns={spec}, nullstr='', dateformat='%Y-%m-%d', "
                f"timestampformat='%Y-%m-%d %H:%M:%S')"
            )

    def full_views(self, con) -> None:
        for name in self.tables:
            con.execute(f"CREATE OR REPLACE VIEW main.{qi(name)} AS SELECT * FROM src.{qi(name)}")

    def replay_views(self, con, key_values: tuple, p_value) -> None:
        sp_cols = self.tables[self.label_table]["columns"]
        con.execute("DROP TABLE IF EXISTS main.__p")
        con.execute(
            f"CREATE TABLE main.__p AS SELECT CAST(? AS {sp_cols[self.pred]}) AS p", [p_value]
        )
        con.execute("DROP TABLE IF EXISTS main.__r")
        sel = ", ".join(f"CAST(? AS {sp_cols[k]}) AS {qi(k)}" for k in self.spine_key)
        con.execute(f"CREATE TABLE main.__r AS SELECT {sel}", list(key_values))
        cmp = "<=" if self.boundary_inclusive else "<"
        p = "(SELECT p FROM main.__p)"
        is_r = " AND ".join(f"{qi(k)} = (SELECT {qi(k)} FROM main.__r)" for k in self.spine_key)
        for name, t in self.tables.items():
            cols = t["columns"]
            unit = t.get("time_unit")
            ev, av = t.get("event_time"), t.get("availability_time")
            preds = []
            if name not in self.static:
                preds.append(f"{qi(ev)} {cmp} {p}")
                if av:
                    preds.append(f"{qi(av)} {cmp} {p}")
            visible = " AND ".join(preds) or "TRUE"
            exprs = []
            for c, ty in cols.items():
                if c in self.outcome.get(name, set()):
                    exprs.append(f"{perturb_expr(c, ty)} AS {qi(c)}")
                elif name == self.label_table and c in self.labels:
                    if self.lka is None:
                        final = "FALSE"
                    else:
                        final = f"{time_shift(qi(ev), cols[ev], unit, self.lka)} {cmp} {p}"
                    exprs.append(
                        f"CASE WHEN ({is_r}) OR NOT ({final}) THEN {perturb_expr(c, ty)} "
                        f"ELSE {qi(c)} END AS {qi(c)}"
                    )
                else:
                    exprs.append(qi(c))
            where = f"({is_r}) OR ({visible})" if name == self.label_table else visible
            con.execute(
                f"CREATE OR REPLACE VIEW main.{qi(name)} AS "
                f"SELECT {', '.join(exprs)} FROM src.{qi(name)} WHERE {where}"
            )

    # -- running --------------------------------------------------------------
    def run_query(self, con, sql: str):
        rel = con.sql(sql)
        cols = [c for c in rel.columns]
        return cols, rel.fetchall()

    def keyed(self, cols, rows, fname):
        need = list(dict.fromkeys(self.spine_key + self.entity_keys + [self.pred] + self.labels))
        missing = [c for c in need if c not in cols]
        if missing:
            raise CaseError(f"{fname}: output lacks {missing}")
        feats = [c for c in cols if c not in need]
        if not feats:
            raise CaseError(f"{fname}: output has no feature column")
        ki = [cols.index(k) for k in self.spine_key]
        fi = [cols.index(f) for f in feats]
        out = {}
        for r in rows:
            k = tuple(r[i] for i in ki)
            if k in out:
                raise CaseError(f"{fname}: duplicate spine key {k}")
            out[k] = tuple(r[i] for i in fi)
        return feats, out


def run_case(case: Case, n_rows: int) -> list[str]:
    case.check_consistency()
    notes = []
    with tempfile.TemporaryDirectory() as tmp:
        data = Path(tmp)
        subprocess.run(
            [sys.executable, str(case.path / "generate.py"), str(data)],
            check=True,
            capture_output=True,
            text=True,
        )
        gen2 = Path(tmp) / "again"
        gen2.mkdir()
        subprocess.run(
            [sys.executable, str(case.path / "generate.py"), str(gen2)],
            check=True,
            capture_output=True,
            text=True,
        )
        for f in data.glob("*.csv"):
            if f.read_bytes() != (gen2 / f.name).read_bytes():
                raise CaseError(f"generator is not deterministic ({f.name})")
        con = duckdb.connect()
        case.load(con, data)
        case.full_views(con)
        counts = {
            n: con.execute(f"SELECT count(*) FROM src.{qi(n)}").fetchone()[0] for n in case.tables
        }
        notes.append("rows " + ", ".join(f"{n}={c}" for n, c in counts.items()))

        spine_sql = (
            f"SELECT {', '.join(qi(k) for k in case.spine_key)}, {qi(case.pred)} "
            f"FROM src.{qi(case.label_table)}"
        )
        if case.spine_filter:
            spine_sql += f" WHERE {case.spine_filter}"
        spine = con.execute(spine_sql).fetchall()
        spine_p = {tuple(r[:-1]): r[-1] for r in spine}
        if len(spine_p) != len(spine):
            raise CaseError("spine key is not unique in the label table")

        full = {}
        for q in case.queries:
            sql = (case.path / q["file"]).read_text()
            cols, rows = case.run_query(con, sql)
            feats, out = case.keyed(cols, rows, q["file"])
            if set(out) != set(spine_p):
                raise CaseError(
                    f"{q['file']}: returned {len(out)} rows for {len(spine_p)} "
                    f"spine rows (or different keys)"
                )
            full[q["file"]] = (sql, feats, out)

        pairs = {}
        for q in case.queries:
            pairs.setdefault(q["pair"], {})[q["label"]] = q
        differing = set()
        for pid, pq in sorted(pairs.items()):
            if set(pq) != {"leaky", "clean"}:
                raise CaseError(f"pair {pid} needs one leaky and one clean query")
            _, lf, lo = full[pq["leaky"]["file"]]
            _, cf, co = full[pq["clean"]["file"]]
            if lf != cf:
                raise CaseError(f"pair {pid}: feature columns differ {lf} vs {cf}")
            diff = {
                k
                for k in lo
                if not all(values_equal(a, b) for a, b in zip(lo[k], co[k], strict=True))
            }
            if not diff:
                raise CaseError(f"pair {pid}: leaky and clean features are identical")
            differing |= diff
            notes.append(f"pair {pid}: features differ on {len(diff)}/{len(lo)} spine rows")

        rng = random.Random(7)
        keys = sorted(spine_p, key=repr)
        dif = sorted(differing, key=repr)
        sample = rng.sample(dif, min(len(dif), n_rows // 2))
        rest = [k for k in keys if k not in set(sample)]
        sample += rng.sample(rest, min(len(rest), n_rows - len(sample)))

        clean_bad = {q["file"]: [] for q in case.queries if q["label"] == "clean"}
        leaky_hit = {q["file"]: 0 for q in case.queries if q["label"] == "leaky"}
        for k in sample:
            case.replay_views(con, k, spine_p[k])
            for q in case.queries:
                sql, feats, out = full[q["file"]]
                cols, rows = case.run_query(con, sql)
                _, rout = case.keyed(cols, rows, q["file"])
                got = rout.get(k)
                same = got is not None and all(
                    values_equal(a, b) for a, b in zip(got, out[k], strict=True)
                )
                if q["label"] == "clean" and not same:
                    clean_bad[q["file"]].append((k, out[k], got))
                if q["label"] == "leaky" and not same:
                    leaky_hit[q["file"]] += 1
        problems = []
        for f, bad in clean_bad.items():
            if bad:
                k, a, b = bad[0]
                problems.append(
                    f"{f}: changes under point-in-time replay on {len(bad)} rows, "
                    f"e.g. key {k}: full {a} vs replay {b}"
                )
        for f, hits in leaky_hit.items():
            if hits == 0:
                problems.append(
                    f"{f}: does not change under point-in-time replay on any sampled row"
                )
        notes.append(
            f"replayed {len(sample)} spine rows; leaky changed on "
            + ", ".join(f"{f}={h}" for f, h in sorted(leaky_hit.items()))
        )
        if problems:
            raise CaseError("; ".join(problems))
        con.close()
    return notes


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("cases", nargs="*", help="case ids (default: all)")
    ap.add_argument("--rows", type=int, default=60, help="spine rows replayed per case")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--cases-dir", type=Path, default=CASES, help="folder of the case folders")
    args = ap.parse_args()
    dirs = sorted(p for p in args.cases_dir.iterdir() if (p / "case.yaml").exists())
    if args.cases:
        dirs = [d for d in dirs if d.name in args.cases]
    failed = 0
    for d in dirs:
        case = Case(d)
        try:
            notes = run_case(case, args.rows)
            print(f"PASS {case.id}")
            if args.verbose:
                for n in notes:
                    print(f"     {n}")
        except (CaseError, duckdb.Error, subprocess.CalledProcessError) as e:
            failed += 1
            msg = e.stderr if isinstance(e, subprocess.CalledProcessError) else str(e)
            print(f"FAIL {case.id}: {msg}")
    print(f"{len(dirs) - failed}/{len(dirs)} cases pass")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
