"""Render SQL templates into labeled leakage cases.

Template format (`templates/<split>/<template_id>.sql`):

    -- leak: clean | L1 | ... | L7
    -- param <name>: value | value | ...      (zero or more lines)
    <SQL with {name} placeholders>

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
from dataclasses import asdict, dataclass, field
from pathlib import Path

from bench.leakage.splits import load_splits

TEMPLATES_DIR = Path(__file__).parent / "templates"
SPLITS = tuple(load_splits())
DEFAULT_SPLITS = tuple(name for name, split in load_splits().items() if split.seen)
LEAK_TYPES = ("clean", "L1", "L2", "L3", "L4", "L5", "L6", "L7")
_HEADER = re.compile(r"^--\s*(leak|param\s+(\w+))\s*:\s*(.*)$")


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

    @property
    def is_leaky(self) -> bool:
        return self.label == "leaky"


def load_template(path: Path, split: str) -> Template:
    leak_type, params, body = None, {}, []
    raw = path.read_bytes()
    for line in raw.decode("utf-8").splitlines():
        match = _HEADER.match(line.strip())
        if match and not body:
            if match.group(1) == "leak":
                leak_type = match.group(3).strip()
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
