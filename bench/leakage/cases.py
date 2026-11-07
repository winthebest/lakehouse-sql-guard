"""Render SQL templates into labeled leakage cases.

Template format (`templates/<split>/<template_id>.sql`):

    -- leak: clean | L1 | ... | L7
    -- output_roles: label=<cols>; metadata=<cols>; features=<cols>   (optional, at most once)
    -- param <name>: value | value | ...      (zero or more lines)
    <SQL with {name} placeholders>

The optional `output_roles` header says which output columns of the query are the label, run
metadata or the only features, as a user would declare them in a dbt `schema.yml`. It is merged
into the split's spec for that template (a role named in the header replaces the spec's list for
that role; the others are kept). A template without the header is checked with its spec as is.

Every combination of parameter values becomes one case. Splits are by template:

- `dev`: tuned on; the rules and thresholds were developed against these.
- `dev_v1b`: the v1 held-out templates. They were seen while the rules were extended, so they are
  development data now and a result on them is in-sample.
- `holdout_v2`: written by independent authors after the v2 rules were frozen. A result was read
  and the rules were fixed against it, so it is development data now and a result on it is
  in-sample.
- `holdout_v3`: written by new authors who never saw `holdout_v2`, after the v3 rules are frozen.
  Empty until then.

`splits.yaml` lists the splits, the spec each is checked with, and which were seen.

Usage: python -m bench.leakage.cases --seed 42 --split dev --out bench/leakage/cases.jsonl
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import random
import re
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

from bench.leakage.splits import load_splits
from sqlguard.leakage.spec import OUTPUT_ROLES, LeakageSpec, OutputRoles, SpecError

TEMPLATES_DIR = Path(__file__).parent / "templates"
SPLITS = tuple(load_splits())
DEFAULT_SPLITS = tuple(name for name, split in load_splits().items() if split.seen)
LEAK_TYPES = ("clean", "L1", "L2", "L3", "L4", "L5", "L6", "L7")
_HEADER = re.compile(r"^--\s*(leak|output_roles|param\s+(\w+))\s*:\s*(.*)$")
_COLUMN = re.compile(r"^[A-Za-z_]\w*$")


@dataclass(frozen=True)
class Template:
    template_id: str
    split: str
    leak_type: str
    params: dict[str, list[str]]
    body: str
    # SHA-256 of the file's bytes as they were read to parse it, so what a result reports about a
    # template is what was scored, not what the file holds when the run ends
    sha256: str = field(default="", compare=False)
    # role -> output columns declared in the `-- output_roles:` header (empty without one)
    output_roles: dict[str, list[str]] = field(default_factory=dict)


@dataclass(frozen=True)
class LeakCase:
    case_id: str
    template_id: str
    split: str
    label: str  # "clean" | "leaky"
    leak_type: str  # "clean" | "L1".."L7"
    params: dict[str, str]
    sql: str
    template_sha256: str = field(default="", compare=False)  # of the template file it came from
    output_roles: dict[str, list[str]] = field(default_factory=dict)  # see Template

    @property
    def is_leaky(self) -> bool:
        return self.label == "leaky"


def parse_output_roles(value: str, where: str) -> dict[str, list[str]]:
    """`label=a; metadata=b, c` as `{"label": ["a"], "metadata": ["b", "c"]}`.

    Columns are lowercased (the spec compares names case-insensitively). Every role needs at
    least one column, a role is named once, and a column has one role.
    """
    roles: dict[str, list[str]] = {}
    for part in value.split(";"):
        role, sep, columns = part.partition("=")
        role = role.strip().lower()
        names = [c.strip().lower() for c in columns.split(",")]
        if not sep or role not in OUTPUT_ROLES:
            raise ValueError(f"{where}: bad output_roles entry {part.strip()!r}: use <role>=<cols>")
        if role in roles:
            raise ValueError(f"{where}: output_roles names {role!r} twice")
        if not all(_COLUMN.match(n) for n in names):
            raise ValueError(f"{where}: output_roles {role!r} needs comma-separated column names")
        roles[role] = names
    columns_seen = [c for names in roles.values() for c in names]
    if len(columns_seen) != len(set(columns_seen)):
        raise ValueError(f"{where}: output_roles lists a column under two roles")
    return roles


def spec_for_case(spec: LeakageSpec, case: LeakCase) -> LeakageSpec:
    """The spec a case is checked with: the split's spec, with the roles its template declared."""
    if not case.output_roles:
        return spec
    declared = {
        "label": spec.output_roles.label,
        "metadata": spec.output_roles.metadata,
        **({"features": spec.output_roles.features} if spec.output_roles.features else {}),
        **case.output_roles,
    }
    try:
        return replace(
            spec, output_roles=OutputRoles.from_value({k: list(v) for k, v in declared.items()})
        )
    except SpecError as err:
        raise ValueError(f"{case.case_id}: output_roles header: {err}") from err


def load_template(path: Path, split: str) -> Template:
    leak_type, params, body = None, {}, []
    roles: dict[str, list[str]] | None = None
    raw = path.read_bytes()
    for line in raw.decode("utf-8").splitlines():
        match = _HEADER.match(line.strip())
        if match and not body:
            if match.group(1) == "leak":
                leak_type = match.group(3).strip()
            elif match.group(1) == "output_roles":
                if roles is not None:
                    raise ValueError(f"{path}: '-- output_roles:' may appear only once")
                roles = parse_output_roles(match.group(3), str(path))
            else:
                params[match.group(2)] = [v.strip() for v in match.group(3).split("|")]
        else:
            body.append(line)
    if leak_type not in LEAK_TYPES:
        raise ValueError(f"{path}: missing or invalid '-- leak:' header ({leak_type!r})")
    return Template(
        path.stem,
        split,
        leak_type,
        params,
        "\n".join(body).strip(),
        hashlib.sha256(raw).hexdigest(),
        roles or {},
    )


def load_templates(split: str, root: Path = TEMPLATES_DIR) -> list[Template]:
    return [load_template(p, split) for p in sorted((root / split).glob("*.sql"))]


def render(template: Template) -> list[LeakCase]:
    names = sorted(template.params)
    combos = itertools.product(*(template.params[n] for n in names)) if names else [()]
    cases = []
    for index, values in enumerate(combos):
        binding = dict(zip(names, values, strict=True))
        cases.append(
            LeakCase(
                case_id=f"{template.template_id}-{index:03d}",
                template_id=template.template_id,
                split=template.split,
                label="clean" if template.leak_type == "clean" else "leaky",
                leak_type=template.leak_type,
                params=binding,
                sql=template.body.format(**binding),
                template_sha256=template.sha256,
                output_roles=template.output_roles,
            )
        )
    return cases


def generate_cases(seed: int = 42, splits: tuple[str, ...] = ("dev",)) -> list[LeakCase]:
    """All cases for `splits`, shuffled deterministically by `seed`."""
    cases = [case for split in splits for t in load_templates(split) for case in render(t)]
    random.Random(seed).shuffle(cases)
    return cases


def write_jsonl(cases: list[LeakCase], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        for case in cases:
            handle.write(json.dumps(asdict(case), sort_keys=True) + "\n")


def read_jsonl(path: Path) -> list[LeakCase]:
    with open(path, encoding="utf-8") as handle:
        return [LeakCase(**json.loads(line)) for line in handle if line.strip()]


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--split", choices=[*SPLITS, "all"], default="dev")
    parser.add_argument("--out", type=Path, default=Path("bench/leakage/cases.jsonl"))
    args = parser.parse_args(argv)
    splits = SPLITS if args.split == "all" else (args.split,)
    cases = generate_cases(args.seed, splits)
    write_jsonl(cases, args.out)
    counts = {t: sum(c.leak_type == t for c in cases) for t in LEAK_TYPES}
    print(f"wrote {len(cases)} cases to {args.out}: {counts}")


if __name__ == "__main__":
    main()
