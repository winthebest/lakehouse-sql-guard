import hashlib
from collections import Counter

import pytest

from bench.leakage.cases import (
    LEAK_TYPES,
    LeakCase,
    generate_cases,
    load_template,
    parse_output_roles,
    read_jsonl,
    render,
    spec_for_case,
    write_jsonl,
)
from sqlguard.core.duck import connect_catalog
from sqlguard.core.parsing import parse_single
from sqlguard.leakage.spec import LeakageSpec


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


# --- the optional `-- output_roles:` header ----------------------------------------------------

BASE_SPEC = {
    "label_table": "labels",
    "entity_keys": ["user_id"],
    "prediction_ts": "label_ts",
    "label_columns": ["churned"],
    "time_columns": {"events": "event_ts"},
}


def write_template(tmp_path, text: str):
    path = tmp_path / "t_example.sql"
    path.write_text(text, encoding="utf-8")
    return load_template(path, "fixture")


def test_a_template_without_the_header_has_no_roles_and_keeps_its_spec(tmp_path):
    template = write_template(tmp_path, "-- leak: clean\nSELECT 1 AS x\n")
    assert template.output_roles == {}
    [case] = render(template)
    assert case.output_roles == {}
    spec = LeakageSpec.from_dict({**BASE_SPEC, "output_roles": {"metadata": ["built_at"]}})
    assert spec_for_case(spec, case) is spec


def test_the_header_declares_roles_like_a_schema_file_would(tmp_path):
    template = write_template(
        tmp_path,
        "-- leak: L1\n"
        "-- output_roles: label=next_return; metadata=_loaded_at, built_at\n"
        "-- param w: 1 | 2\n"
        "SELECT {w} AS x\n",
    )
    assert template.output_roles == {
        "label": ["next_return"],
        "metadata": ["_loaded_at", "built_at"],
    }
    cases = render(template)
    assert len(cases) == 2 and all(c.output_roles == template.output_roles for c in cases)
    assert all("output_roles" not in c.sql for c in cases)  # the header is not part of the query
    spec = spec_for_case(LeakageSpec.from_dict(BASE_SPEC), cases[0])
    assert spec.output_roles.label == ("next_return",)
    assert spec.output_roles.metadata == ("_loaded_at", "built_at")
    assert spec.label_columns == ("churned",)  # the rest of the spec is untouched


def test_the_header_overrides_one_role_and_keeps_the_others_of_the_spec(tmp_path):
    base = LeakageSpec.from_dict(
        {**BASE_SPEC, "output_roles": {"label": ["a"], "metadata": ["m1"]}}
    )
    template = write_template(tmp_path, "-- leak: clean\n-- output_roles: metadata=m2\nSELECT 1\n")
    merged = spec_for_case(base, render(template)[0])
    assert (merged.output_roles.label, merged.output_roles.metadata) == (("a",), ("m2",))


def test_roles_are_case_insensitive_like_the_spec(tmp_path):
    template = write_template(
        tmp_path, "-- leak: clean\n-- output_roles: Label=Next_Return\nSELECT 1\n"
    )
    spec = spec_for_case(LeakageSpec.from_dict(BASE_SPEC), render(template)[0])
    assert spec.output_roles.label == ("next_return",)


def test_a_role_that_clashes_with_the_spec_is_an_error_naming_the_case(tmp_path):
    template = write_template(
        tmp_path, "-- leak: clean\n-- output_roles: label=user_id\nSELECT 1\n"
    )
    with pytest.raises(ValueError, match=r"t_example-000.*user_id"):
        spec_for_case(LeakageSpec.from_dict(BASE_SPEC), render(template)[0])


@pytest.mark.parametrize(
    "value",
    [
        "",
        "label",
        "label=",
        "label=a; label=b",
        "labels=a",
        "features=",
        "label=a; metadata=a",
        "label=a b",
        "label=a,,b",
        "label=1a",
        "label=a;;",
    ],
)
def test_a_malformed_header_is_refused_with_the_file_name(tmp_path, value):
    with pytest.raises(ValueError, match="t_example"):
        write_template(tmp_path, f"-- leak: clean\n-- output_roles: {value}\nSELECT 1\n")


def test_the_header_may_appear_only_once(tmp_path):
    with pytest.raises(ValueError, match="once"):
        write_template(
            tmp_path,
            "-- leak: clean\n-- output_roles: label=a\n-- output_roles: metadata=b\nSELECT 1\n",
        )


def test_a_header_after_the_query_started_is_an_ordinary_comment(tmp_path):
    template = write_template(tmp_path, "-- leak: clean\nSELECT 1\n-- output_roles: label=a\n")
    assert template.output_roles == {} and "-- output_roles" in template.body


def test_parse_output_roles_accepts_the_features_role():
    assert parse_output_roles("features=f1,f2", "x") == {"features": ["f1", "f2"]}


def test_roles_survive_the_jsonl_roundtrip(tmp_path):
    template = write_template(tmp_path, "-- leak: clean\n-- output_roles: label=a\nSELECT 1\n")
    path = tmp_path / "cases.jsonl"
    write_jsonl(render(template), path)
    [case] = read_jsonl(path)
    assert isinstance(case, LeakCase) and case.output_roles == {"label": ["a"]}
