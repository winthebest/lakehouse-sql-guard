"""The v4 variant of the Kaggle cases: each case's spec with the integer time unit declared.

    uv run python -m bench.kaggle.make_variants [--check]

`bench/kaggle/variants/v4/<case>/spec.yaml` is the case's spec.yaml byte for byte plus one added
field, `time_units`, copied from the `time_unit` of its tables in schema.yaml (`s` is `seconds`,
`ms` is `milliseconds`). That is the only change: no table, column, label, duration or query is
touched, and a case with no integer time column gets an identical copy. `--check` writes nothing
and fails when a committed variant is not what this file generates.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
CASES = HERE / "cases"
VARIANTS = HERE / "variants" / "v4"
UNITS = {"s": "seconds", "ms": "milliseconds"}


class VariantError(ValueError):
    """A case whose time unit cannot be written into its spec."""


def declared_units(schema: dict, spec: dict) -> dict[str, str]:
    """`{table: seconds|milliseconds}` for every table of schema.yaml that gives a `time_unit`."""
    listed = {str(t).strip().lower() for t in spec.get("time_columns") or {}}
    units: dict[str, str] = {}
    for name, table in (schema.get("tables") or {}).items():
        unit = table.get("time_unit") if isinstance(table, dict) else None
        if unit is None:
            continue
        if unit not in UNITS:
            raise VariantError(f"table {name!r}: time_unit {unit!r} is not one of {sorted(UNITS)}")
        if str(name).strip().lower() not in listed:
            raise VariantError(f"table {name!r} has a time_unit but no entry in time_columns")
        units[str(name)] = UNITS[unit]
    return units


def variant_text(spec_text: str, schema: dict) -> str:
    """The spec text of the variant: `spec_text` unchanged, then the `time_units` field."""
    spec = yaml.safe_load(spec_text)
    if "time_units" in spec:
        raise VariantError("the spec already declares time_units")
    units = declared_units(schema, spec)
    if not units:
        return spec_text
    body = spec_text if spec_text.endswith("\n") else spec_text + "\n"
    inline = ", ".join(f"{table}: {unit}" for table, unit in units.items())
    note = "# added for the v4 variant: the unit of each integer time column (schema.yaml)"
    return f"{body}{note}\ntime_units: {{{inline}}}\n"


def render(cases: Path = CASES) -> dict[str, str]:
    found = {}
    for case in sorted(p for p in cases.iterdir() if (p / "case.yaml").exists()):
        schema = yaml.safe_load((case / "schema.yaml").read_text(encoding="utf-8"))
        found[case.name] = variant_text((case / "spec.yaml").read_text(encoding="utf-8"), schema)
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="write nothing; compare only")
    args = parser.parse_args(argv)
    stale = []
    for case, text in render().items():
        path = VARIANTS / case / "spec.yaml"
        if args.check:
            if not path.is_file() or path.read_text(encoding="utf-8") != text:
                stale.append(case)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    if stale:
        print(f"variants out of date: {', '.join(stale)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
