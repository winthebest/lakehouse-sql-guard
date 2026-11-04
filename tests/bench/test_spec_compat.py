"""A spec written for the first rule set keeps giving the verdicts it gave."""

import json
from pathlib import Path

import yaml

from bench.leakage.cases import generate_cases
from bench.leakage.run import SYNTHETIC_SPEC
from sqlguard.leakage.spec import LeakageSpec
from sqlguard.leakage.validator import validate

SNAPSHOT = Path(__file__).parent / "data" / "dev_v1_static_verdicts.json"
NEW_FIELDS = ("boundary", "max_ingest_delay", "label_known_after", "relations")


def v1_spec() -> LeakageSpec:
    data = yaml.safe_load(SYNTHETIC_SPEC.read_text(encoding="utf-8"))
    for key in NEW_FIELDS:
        data.pop(key, None)
    return LeakageSpec.from_dict(data)


def test_dev_verdicts_of_a_v1_spec_are_unchanged(world):
    """Verdict, score, and rules per case, recorded with the first rule set on the dev templates
    that existed then."""
    catalog, _ = world
    schema, spec = catalog.schema_for(), v1_spec()
    expected = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    cases = {c.case_id: c for c in generate_cases(seed=42, splits=("dev",))}
    assert set(expected) <= set(cases) and len(expected) == 228
    changed = {}
    for case_id, (verdict, score, rules) in expected.items():
        report = validate(cases[case_id].sql, spec, schema=schema)
        got = [report.verdict, round(report.score, 4), sorted({f.rule_id for f in report.findings})]
        if got != [verdict, score, rules]:
            changed[case_id] = (got, [verdict, score, rules])
    assert not changed


def test_the_bench_spec_only_adds_to_the_v1_spec():
    full = yaml.safe_load(SYNTHETIC_SPEC.read_text(encoding="utf-8"))
    assert set(NEW_FIELDS) & set(full) == {"max_ingest_delay", "label_known_after"}
    assert LeakageSpec.from_dict(full).boundary == "strict"
