"""How often does `suggest_fix` repair a leaky case, and how far is the repair verified?

Every leaky case of the chosen splits (L6 and L7 are explained, never rewritten, so they are
left out of the rates) goes through `suggest_fix` with the replayer. A case counts at the level of
its last rewrite:

- `static_and_replay`: the repaired query is statically clean and replays point-in-time;
- `static_only`: statically clean, replay could not confirm it;
- `unverified`: a rewrite was offered but the checker or replay disagrees;
- `none`: nothing was rewritten (explanation only).

Each case is also counted by the `semantics` of its rewrite (`preserved`: only a bound was added or
made strict; `changed`: something else changes, with a caveat; `unknown`: nothing was rewritten).
It is independent of the level: a replay PASS shows a repaired query is point-in-time, not that it
computes the same thing.

Also checked on the current data, as the set of prediction rows and how often each comes back: a
repaired query that loses a row the original returned needs a `rows may drop:` caveat, one that
gains rows or repeats them differently needs a row caveat, and a `preserved` fix may not change
the rows at all. This is checked on both CLI paths, with and without replay.

Each split is checked with the spec `splits.yaml` gives it. `dev` and `dev_v1b` were both seen while
the rewrites were written, so their rates are in-sample.

Usage: python -m bench.leakage.fixes_eval --splits dev dev_v1b --out-dir <dir>
"""

from __future__ import annotations

import argparse
import json
import time
from collections import Counter, defaultdict
from pathlib import Path

from bench.leakage.cases import SPLITS, LeakCase, generate_cases
from bench.leakage.splits import load_splits
from bench.leakage.world import synthetic_world
from sqlguard.core.catalog import LakehouseCatalog
from sqlguard.leakage.fixes import Semantics, Verification, key_counts, suggest_fix
from sqlguard.leakage.replay import Replayer
from sqlguard.leakage.rewrite import ROWS_CHANGE, ROWS_KEPT, ROWS_MAY_DROP
from sqlguard.leakage.spec import LeakageSpec
from sqlguard.leakage.validator import Weights, validate

LEVELS = (*[v.value for v in Verification], "none")
SEMANTICS = tuple(v.value for v in Semantics)
NOT_REWRITTEN = ("L6", "L7")
ROW_CAVEATS = (ROWS_MAY_DROP, ROWS_CHANGE, ROWS_KEPT)


def _outcome(fixes: list, case: LeakCase, spec: LeakageSpec, replayer: Replayer) -> dict:
    """Level, meaning and row check of one case on one path.

    Compared on the current data: which prediction rows come back, and how many times. Losing a
    row needs a `rows may drop:` caveat; gaining one or returning rows a different number of times
    needs some row caveat; and any change at all means the fix cannot be `preserved`.
    """
    rewrites = [f for f in fixes if f.rewrites]
    lost_without_caveat = changed_without_caveat = preserved_but_changed = False
    if rewrites:
        before = key_counts(replayer, spec, case.sql)
        after = key_counts(replayer, spec, rewrites[-1].after_sql)
        caveats = [c for f in rewrites for c in f.caveats]
        if before is not None and after is not None and before != after:
            if before.keys() - after.keys():
                lost_without_caveat = not any(c.startswith(ROWS_MAY_DROP) for c in caveats)
            else:
                changed_without_caveat = not any(c.startswith(ROW_CAVEATS) for c in caveats)
            preserved_but_changed = all(f.semantics is Semantics.PRESERVED for f in rewrites)
    if not rewrites:
        semantics = Semantics.UNKNOWN
    elif any(f.semantics is Semantics.CHANGED for f in rewrites):
        semantics = Semantics.CHANGED  # the whole repair: `changed` as soon as one rewrite is
    else:
        semantics = Semantics.PRESERVED
    return {
        "level": rewrites[-1].verification.value if rewrites else "none",
        "semantics": semantics.value,
        "rewrites": len(rewrites),
        "lost_rows_without_caveat": lost_without_caveat,
        "changed_rows_without_caveat": changed_without_caveat,
        "preserved_but_rows_changed": preserved_but_changed,
    }


def evaluate_case(
    case: LeakCase, catalog: LakehouseCatalog, spec: LeakageSpec, replayer: Replayer
) -> dict:
    """Both paths of the CLI: `--suggest-fix` alone (schema only, nothing replayed) and with
    `--warehouse --replay`. Replay may add evidence; it must not be what produces a label or a
    row-loss caveat, so a drop without one fails on either path."""
    schema = catalog.schema_for()
    static = suggest_fix(case.sql, spec, schema=schema)
    report = validate(case.sql, spec, schema=schema, replay=replayer)
    replayed = suggest_fix(case.sql, spec, report, schema=schema, catalog=catalog, replay=replayer)
    on_replay = _outcome(replayed, case, spec, replayer)
    on_static = _outcome(static, case, spec, replayer)
    return {
        "case_id": case.case_id,
        "template_id": case.template_id,
        "leak_type": case.leak_type,
        **on_replay,
        "static": on_static,
        # the label must not depend on whether replay ran
        "semantics_differs": on_static["semantics"] != on_replay["semantics"],
    }


def _summary(records: list[dict]) -> dict:
    counts = Counter(r["level"] for r in records)
    meanings = Counter(r["semantics"] for r in records)
    both: dict[str, Counter] = defaultdict(Counter)
    by_type: dict[str, Counter] = defaultdict(Counter)
    for r in records:
        both[r["semantics"]][r["level"]] += 1
        by_type[r["leak_type"]][r["level"]] += 1
    n = len(records)
    return {
        "suggestion_rate": round(sum(r["rewrites"] > 0 for r in records) / n, 4) if n else None,
        "rates": {lv: round(counts[lv] / n, 4) if n else None for lv in LEVELS},
        "counts": {lv: counts[lv] for lv in LEVELS},
        "semantics_rates": {m: round(meanings[m] / n, 4) if n else None for m in SEMANTICS},
        "semantics_counts": {m: meanings[m] for m in SEMANTICS},
        "level_by_semantics": {m: {lv: both[m][lv] for lv in LEVELS} for m in SEMANTICS},
        "by_type": {t: {lv: c[lv] for lv in LEVELS} for t, c in sorted(by_type.items())},
        "lost_rows_without_caveat": [
            r["case_id"] for r in records if r["lost_rows_without_caveat"]
        ],
        "changed_rows_without_caveat": [
            r["case_id"] for r in records if r["changed_rows_without_caveat"]
        ],
        "preserved_but_rows_changed": [
            r["case_id"] for r in records if r["preserved_but_rows_changed"]
        ],
    }


def evaluate(
    cases: list[LeakCase], catalog: LakehouseCatalog, spec: LeakageSpec, seed: int
) -> dict:
    replayer = Replayer(catalog, spec, max_points=24, seed=seed)
    records = [
        evaluate_case(case, catalog, spec, replayer)
        for case in cases
        if case.is_leaky and case.leak_type not in NOT_REWRITTEN
    ]
    with_replay = _summary(records)
    without_replay = _summary(
        [{**r["static"], "case_id": r["case_id"], "leak_type": r["leak_type"]} for r in records]
    )
    return {
        "n_cases": len(records),
        **with_replay,  # the replay path, as before
        "static_path": without_replay,
        # a drop on either path without a row-loss caveat
        # a failure on either path
        **{
            key: sorted({*with_replay[key], *without_replay[key]})
            for key in (
                "lost_rows_without_caveat",
                "changed_rows_without_caveat",
                "preserved_but_rows_changed",
            )
        },
        "semantics_differs": [r["case_id"] for r in records if r["semantics_differs"]],
        "not_fully_verified": sorted(
            {r["template_id"] for r in records if r["level"] != Verification.STATIC_AND_REPLAY}
        ),
    }


def markdown(result: dict) -> str:
    lines = [
        "# Fix suggestions on the labelled leaky cases (in-sample)",
        "",
        "L6 and L7 are explained, not rewritten, and are not counted. Rates are shares of the "
        "leaky L1-L5 cases by the verification of the last rewrite.",
        "",
        "The level says the repaired query is point-in-time; `semantics` says whether it still",
        "computes the same thing (`changed` always comes with a caveat).",
        "",
        "| split | cases | suggested | "
        + " | ".join(LEVELS)
        + " | "
        + " | ".join(f"semantics {m}" for m in SEMANTICS)
        + " |",
        "|---|---|---|" + "---|" * (len(LEVELS) + len(SEMANTICS)),
    ]
    for split, r in result["splits"].items():
        rates = " | ".join(f"{r['rates'][lv]:.3f}" for lv in LEVELS)
        meaning = " | ".join(f"{r['semantics_rates'][m]:.3f}" for m in SEMANTICS)
        lines.append(
            f"| {split} | {r['n_cases']} | {r['suggestion_rate']:.3f} | {rates} | {meaning} |"
        )
    for split, r in result["splits"].items():
        lines += ["", f"## {split}", "", "| leak type | " + " | ".join(LEVELS) + " |"]
        lines.append("|---|" + "---|" * len(LEVELS))
        for leak, counts in r["by_type"].items():
            lines.append(f"| {leak} | " + " | ".join(str(counts[lv]) for lv in LEVELS) + " |")
        lines += ["", "| level \\ semantics | " + " | ".join(SEMANTICS) + " |"]
        lines.append("|---|" + "---|" * len(SEMANTICS))
        for lv in LEVELS[:-1]:
            row = " | ".join(str(r["level_by_semantics"][m][lv]) for m in SEMANTICS)
            lines.append(f"| {lv} | {row} |")
        static = r["static_path"]
        levels = ", ".join(f"{lv} {static['counts'][lv]}" for lv in LEVELS)
        meanings = ", ".join(f"{m} {static['semantics_counts'][m]}" for m in SEMANTICS)
        lines += [
            "",
            "Without replay (`--suggest-fix` alone, schema only), cases by level: "
            + levels
            + "; by semantics: "
            + meanings,
        ]
        lines += [
            "",
            "Rows lost without a `rows may drop:` caveat (either path): "
            + (", ".join(r["lost_rows_without_caveat"]) or "none"),
            "",
            "Rows gained or repeated differently without a row caveat (either path): "
            + (", ".join(r["changed_rows_without_caveat"]) or "none"),
            "",
            "`preserved` fixes whose rows changed (either path): "
            + (", ".join(r["preserved_but_rows_changed"]) or "none"),
            "",
            "Semantics differs between the paths: " + (", ".join(r["semantics_differs"]) or "none"),
        ]
        lines += [
            "",
            "Templates not fully verified: " + (", ".join(r["not_fully_verified"]) or "none"),
        ]
    return "\n".join(lines) + "\n"


def run(seed: int, warehouse: Path, out_dir: Path, splits: tuple[str, ...]) -> Path:
    manifest = load_splits()
    catalog = synthetic_world(warehouse, seed)
    started = time.perf_counter()
    result = {
        "seed": seed,
        "weights": Weights.load().rules,
        "splits": {
            split: evaluate(
                generate_cases(seed, (split,)),
                catalog,
                LeakageSpec.from_yaml(manifest[split].spec),
                seed,
            )
            for split in splits
        },
    }
    result["runtime_seconds"] = round(time.perf_counter() - started, 1)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "fixes.md").write_text(markdown(result), encoding="utf-8")
    path = out_dir / "fixes.json"
    path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m bench.leakage.fixes_eval", description=__doc__)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--warehouse", type=Path, default=Path(".warehouse/bench"))
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--splits", nargs="+", default=list(SPLITS), choices=SPLITS)
    args = parser.parse_args(argv)
    print(f"results written to {run(args.seed, args.warehouse, args.out_dir, tuple(args.splits))}")


if __name__ == "__main__":
    main()
