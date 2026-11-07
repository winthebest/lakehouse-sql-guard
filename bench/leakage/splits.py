"""Which template splits exist, which spec each is checked with, and which were seen.

The manifest (`splits.yaml`) is the single place that says a split was seen while the rules
were developed; nothing else in the benchmark hard-codes split names.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

MANIFEST = Path(__file__).parent / "splits.yaml"
WORLDS = ("w1", "w2")  # w1: the churn world; w2: the event-log world (bench/leakage/world.py)


@dataclass(frozen=True)
class Split:
    name: str
    spec: Path  # absolute path of the LeakageSpec file
    seen: bool  # the rules were developed while looking at this split
    world: str = "w1"  # the synthetic world the split's queries run on (see WORLDS)


def load_splits(path: Path = MANIFEST) -> dict[str, Split]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    entries = data.get("splits") if isinstance(data, dict) else None
    if not isinstance(entries, dict) or not entries:
        raise ValueError(f"{path}: expected a non-empty `splits` mapping")
    splits = {}
    for name, entry in entries.items():
        if not isinstance(entry, dict) or not {"spec", "seen"} <= set(entry) <= {
            "spec",
            "seen",
            "world",
        }:
            raise ValueError(
                f"{path}: split {name!r} needs exactly `spec` and `seen`, plus an optional `world`"
            )
        world = entry.get("world", "w1")
        if world not in WORLDS:
            raise ValueError(f"{path}: split {name!r}: `world` must be one of {WORLDS}")
        if not isinstance(entry["seen"], bool):
            raise ValueError(f"{path}: split {name!r}: `seen` must be true or false")
        spec = (path.parent / str(entry["spec"])).resolve()
        if not spec.is_file():
            raise ValueError(f"{path}: split {name!r}: spec file {entry['spec']!r} not found")
        splits[str(name)] = Split(str(name), spec, entry["seen"], world)
    return splits


def seen_splits(prereg: dict | None = None, path: Path = MANIFEST) -> frozenset[str]:
    """Splits that count as in-sample: those the manifest marks seen, plus any the
    pre-registration lists in `seen_splits`. Either source is enough, so a split is never reported
    as held-out because one of them is out of date."""
    found = {name for name, split in load_splits(path).items() if split.seen}
    return frozenset(found | set((prereg or {}).get("seen_splits", ())))
