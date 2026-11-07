"""The v4 variant of the Kaggle cases changes one thing: the declared time unit."""

import pytest
import yaml

from bench.kaggle import make_variants as variants
from sqlguard.leakage.spec import LeakageSpec

SPEC = (
    "label_table: t\nentity_keys: [id]\nprediction_ts: ts\nlabel_columns: [y]\n"
    "time_columns: {t: ts}\n"
)


def schema(**tables):
    return {
        "tables": {name: {"columns": {"ts": "BIGINT"}, **extra} for name, extra in tables.items()}
    }


def test_the_unit_is_appended_and_nothing_else_changes():
    text = variants.variant_text(SPEC, schema(t={"time_unit": "ms"}))
    assert text.startswith(SPEC)
    before, after = yaml.safe_load(SPEC), yaml.safe_load(text)
    assert after.pop("time_units") == {"t": "milliseconds"}
    assert after == before


def test_a_case_without_an_integer_time_column_is_copied_as_it_is():
    assert variants.variant_text(SPEC, schema(t={})) == SPEC


@pytest.mark.parametrize(
    ("spec", "tables", "message"),
    [
        (SPEC + "time_units: {t: seconds}\n", {"t": {"time_unit": "s"}}, "already declares"),
        (SPEC, {"t": {"time_unit": "days"}}, "not one of"),
        (SPEC, {"u": {"time_unit": "s"}}, "no entry in time_columns"),
    ],
)
def test_what_cannot_be_written_is_refused(spec, tables, message):
    with pytest.raises(variants.VariantError, match=message):
        variants.variant_text(spec, schema(**tables))


def test_the_committed_variants_are_what_the_generator_writes():
    assert variants.main(["--check"]) == 0


def test_every_variant_spec_loads_and_differs_from_the_case_spec_only_by_time_units():
    for case, text in variants.render().items():
        original = (variants.CASES / case / "spec.yaml").read_text(encoding="utf-8")
        assert text.startswith(original)
        LeakageSpec.from_dict(yaml.safe_load(text))  # the checker accepts every variant
        extra = {
            k: v for k, v in yaml.safe_load(text).items() if yaml.safe_load(original).get(k) != v
        }
        assert set(extra) <= {"time_units"}, case
