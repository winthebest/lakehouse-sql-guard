"""Shape checks for the dev_v1b and real-data templates (no detector is involved)."""

import re
from collections import Counter

import sqlglot

from bench.leakage.cases import LEAK_TYPES, generate_cases, load_templates, render
from sqlguard.core.duck import connect_catalog

V1_TYPES = LEAK_TYPES[1:7]  # L1..L6: the leak types of the first rule set
SPINE_TS = {"label_ts", "cutoff_ts"}
FEATURE_TS = re.compile(r"(_ts|_at|_as_of)$")


def _dev_v1b():
    return generate_cases(seed=42, splits=("dev_v1b",))


def _feature_timestamp_outputs(sql: str) -> set[str]:
    outputs = sqlglot.parse_one(sql, read="duckdb").named_selects
    return {name for name in outputs if FEATURE_TS.search(name) and name not in SPINE_TS}


def test_dev_v1b_size_and_mix():
    cases = _dev_v1b()
    counts = Counter(c.leak_type for c in cases)
    assert len(cases) >= 100
    assert {c.split for c in cases} == {"dev_v1b"}
    assert 0.40 <= counts["clean"] / len(cases) <= 0.50
    assert all(counts[t] >= 8 for t in V1_TYPES)


def test_dev_v1b_cases_are_unique_and_disjoint_from_dev():
    v1b, dev = _dev_v1b(), generate_cases(seed=42, splits=("dev",))
    assert len({c.case_id for c in v1b}) == len(v1b)
    assert not {c.template_id for c in v1b} & {c.template_id for c in dev}
    assert not {c.sql for c in v1b} & {c.sql for c in dev}


def test_dev_v1b_cases_parse():
    for case in _dev_v1b():
        sqlglot.parse_one(case.sql, read="duckdb")


def test_dev_v1b_has_feature_timestamp_outputs():
    with_ts = [c for c in _dev_v1b() if _feature_timestamp_outputs(c.sql)]
    assert len(with_ts) >= 6
    assert {c.label for c in with_ts} == {"clean", "leaky"}


def test_dev_v1b_cases_run_and_return_rows(world):
    catalog, _ = world
    conn = connect_catalog(catalog)
    for case in _dev_v1b():
        assert conn.sql(case.sql).fetchall(), case.case_id


def test_real_templates_render_and_parse():
    templates = load_templates("real")
    cases = [case for t in templates for case in render(t)]
    counts = Counter(c.leak_type for c in cases)
    assert 8 <= len(templates) <= 12
    assert counts["clean"] >= 4
    assert all(counts[t] >= 1 for t in V1_TYPES)
    for case in cases:
        sqlglot.parse_one(case.sql, read="duckdb")
