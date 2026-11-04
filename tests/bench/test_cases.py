import hashlib
from collections import Counter

from bench.leakage.cases import LEAK_TYPES, generate_cases, read_jsonl, write_jsonl
from sqlguard.core.duck import connect_catalog
from sqlguard.core.parsing import parse_single


def test_dev_case_counts_and_balance():
    cases = generate_cases(seed=42)
    counts = Counter(c.leak_type for c in cases)
    assert len(cases) >= 200
    assert all(counts[t] >= 20 for t in LEAK_TYPES[:7])  # clean, L1..L6
    assert counts["L7"] >= 8
    assert 0.4 <= counts["clean"] / len(cases) <= 0.6
    assert {c.split for c in cases} == {"dev"}


def test_split_is_by_template():
    templates = Counter(c.template_id for c in generate_cases())
    assert all(tid.startswith(("c", "l")) for tid in templates)
    ids = [c.case_id for c in generate_cases()]
    assert len(ids) == len(set(ids))


def test_deterministic_output(tmp_path):
    def digest(seed):
        path = tmp_path / f"cases-{seed}.jsonl"
        write_jsonl(generate_cases(seed), path)
        return hashlib.sha256(path.read_bytes()).hexdigest()

    assert digest(42) == digest(42)
    assert digest(42) != digest(43)  # seed controls order


def test_jsonl_roundtrip(tmp_path):
    cases = generate_cases()
    path = tmp_path / "cases.jsonl"
    write_jsonl(cases, path)
    assert read_jsonl(path) == cases


def test_every_case_parses_and_runs(world):
    catalog, _ = world
    conn = connect_catalog(catalog)
    for case in generate_cases():
        parse_single(case.sql)
        conn.sql(case.sql).fetchall()
