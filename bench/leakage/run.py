"""Leakage benchmark: generate cases -> score with four detectors -> metrics -> report.

Detectors
- baseline: the course brief's check (an output feature timestamp later than prediction_ts)
- static:   sqlguard static rules only
- replay:   point-in-time replay only (score = fraction of rows that differ)
- combined: static rules + replay (the shipped validator)

A detector that cannot check a query (unparseable, undeclared table, spec mismatch, expired
history, too little replay evidence, or a static L4 hint that only replay can settle) reports
`unverifiable`. The product fails closed on those; the benchmark scores them as the detector's
score says (an analysis crash scores 1.0, an unsettled hint scores below the cut) and counts and
lists them separately (`n_unverifiable`, `unverifiable_cases`) so they are never hidden inside
the FPR or recall.

Rows of the comparison (static-only and combined for each):
- (a) v1: the detector of git tag `v0.1.1`, run from its own virtualenv (see `v1_runner`);
- (b) v2 with a spec that has only the fields v1 knew;
- (c) v2 with the full spec (the rows named `static`, `replay`, `combined`);
- the baseline.
A query a row cannot read scores "not detected" and is listed with the reason.

Protocol: `prereg` picks each detector's threshold on the dev split (FPR <= 5%) and writes it
with the commit SHA, the splits it had seen, and a hash of every spec; `run --splits ...`
evaluates the named splits with those thresholds, each with the spec `splits.yaml` gives it.
Which splits are in-sample comes from the pre-registration and `splits.yaml`, never from a list
in this file: a result only counts as held-out for a split written after the rules and the spec
were frozen.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import yaml

from bench.leakage import optimism
from bench.leakage.baseline import baseline_score, bind_relations
from bench.leakage.cases import (
    DEFAULT_SPLITS,
    LEAK_TYPES,
    SPLITS,
    TEMPLATES_DIR,
    LeakCase,
    generate_cases,
    load_templates,
    render,
)
from bench.leakage.freeze import (
    ROOT,
    checkout_state,
    frozen_hashes,
    frozen_mismatches,
    is_ancestor,
)
from bench.leakage.metrics import auc, bootstrap_ci, confusion, roc_curve, threshold_at_fpr
from bench.leakage.splits import load_splits, seen_splits
from bench.leakage.v1_runner import (
    V1_TAG,
    V1Score,
    apply_v1_taxonomy,
    describe_unverifiable,
    ensure_v1_python,
    run_v1,
    tag_commit,
    v1_spec_data,
)
from bench.leakage.world import synthetic_world, world_dir
from sqlguard.core.catalog import LakehouseCatalog
from sqlguard.leakage.replay import Replayer, ReplayStatus
from sqlguard.leakage.spec import LeakageSpec
from sqlguard.leakage.validator import UNCHECKED, LeakageReport, Weights, validate

DETECTORS = ("baseline", "static", "replay", "combined")  # the baseline and row (c)
V1_ROWS = ("v1_static", "v1_combined")  # row (a)
V1_SPEC_ROWS = ("v2v1_static", "v2v1_combined")  # row (b)
ALL_ROWS = (*DETECTORS, *V1_ROWS, *V1_SPEC_ROWS)
ROW_NAMES = {
    "static": "(c) v2 static",
    "replay": "(c) v2 replay",
    "combined": "(c) v2 combined",
    "v1_static": "(a) v1 static",
    "v1_combined": "(a) v1 combined",
    "v2v1_static": "(b) v2, v1 spec fields, static",
    "v2v1_combined": "(b) v2, v1 spec fields, combined",
}
SYNTHETIC_SPEC = Path(__file__).parent / "spec.yaml"
RESULTS_DIR = Path("results/leakage")
MAX_FPR = 0.05
OPTIMISM_CUTOFF = datetime(2026, 1, 24)


@dataclass
class Scored:
    case: LeakCase
    scores: dict[str, float]
    leak_types: list[str]
    seconds: dict[str, float]
    verdicts: dict[str, str] = field(default_factory=dict)  # detector -> "unverifiable" if so
    reasons: dict[str, str] = field(default_factory=dict)  # detector -> why it is unverifiable
    v1: V1Score | None = None  # what v1 said, with the score it gave before "not detected"


# Written into the pre-registration, so a result that matches them is reported as anticipated.
EXPECTATIONS = (
    "Static-only recall on SCD2 (plan_history) L4 templates is expected to be near zero: the "
    "static rules do not know which column closes a version, so every use of valid_to (the as-of "
    "lookup valid_to >= t, tenure features, counts of closed versions) is statically clean. "
    "Replay catches them; the combined detector's recall on them is the number that matters.",
    "A cutoff read as MAX(label_ts) over the whole spine, and a label counted final exactly "
    "`horizon` old, are leaky under the label audit's strict conventions; the detector treats a "
    "dataset-level time split as legitimate. Disagreements on such templates are expected.",
)


def git_sha() -> tuple[str, bool]:
    """`(short sha, dirty)` of this repository for labelling a result; ("unknown", False) outside
    a checkout. A held-out run and `prereg` do not use it: they refuse outside a checkout."""
    try:
        return checkout_state()
    except SystemExit:
        return "unknown", False


def unverifiable_reason(report: LeakageReport) -> str:
    """Why a validator report is `unverifiable` (empty for any other verdict)."""
    return describe_unverifiable(report, UNCHECKED)


def score_cases(
    cases: list[LeakCase],
    catalog: LakehouseCatalog,
    spec: LeakageSpec,
    seed: int,
    *,
    v1_spec: LeakageSpec | None = None,
    v1_scores: list[V1Score] | None = None,
) -> list[Scored]:
    """Score `cases` with the baseline and row (c); also row (b) when `v1_spec` (the spec without
    the fields added after v1) is given, and row (a) when `v1_scores` (one per case) is given."""
    schema = catalog.schema_for()
    replayer = Replayer(catalog, spec, max_points=24, seed=seed)
    ablation = Replayer(catalog, v1_spec, max_points=24, seed=seed) if v1_spec else None
    current = replayer.current()
    bind_relations(current, spec)
    weights = Weights.load()
    scored = []
    for position, case in enumerate(cases):
        seconds = {}
        t = time.perf_counter()
        base = baseline_score(current, case.sql, spec)
        seconds["baseline"] = time.perf_counter() - t
        t = time.perf_counter()
        static = validate(case.sql, spec, schema=schema, weights=weights)
        seconds["static"] = time.perf_counter() - t
        t = time.perf_counter()
        replay = replayer.run(case.sql)
        seconds["replay"] = time.perf_counter() - t
        combined = validate(case.sql, spec, schema=schema, replay=replayer, weights=weights)
        replay_score = replay.mismatch_rate if replay.status is ReplayStatus.MISMATCH else 0.0
        scores = {
            "baseline": base,
            "static": static.score,
            "replay": replay_score,
            "combined": combined.score,
        }
        reasons = {
            "static": unverifiable_reason(static),
            "combined": unverifiable_reason(combined),
            "replay": replay.reason if replay.status is ReplayStatus.UNVERIFIABLE else "",
        }
        if ablation is not None:
            static_b = validate(case.sql, v1_spec, schema=schema, weights=weights)
            combined_b = validate(
                case.sql, v1_spec, schema=schema, replay=ablation, weights=weights
            )
            scores |= {"v2v1_static": static_b.score, "v2v1_combined": combined_b.score}
            reasons |= {
                "v2v1_static": unverifiable_reason(static_b),
                "v2v1_combined": unverifiable_reason(combined_b),
            }
        v1_applied = None
        if v1_scores is not None:
            v1_applied = apply_v1_taxonomy(v1_scores[position], case.leak_type)
            scores |= {"v1_static": v1_applied.static, "v1_combined": v1_applied.combined}
            reasons |= {f"v1_{d}": r for d, r in v1_applied.reasons.items()}
        reasons = {d: r for d, r in reasons.items() if r}
        scored.append(
            Scored(
                case,
                scores,
                combined.leak_types,
                seconds,
                {d: "unverifiable" for d in reasons},
                reasons,
                v1_applied,
            )
        )
    return scored


def rows_of(scored: list[Scored], only: dict | None = None) -> list[str]:
    """Rows scored for every case (and, with `only`, that have a threshold in it)."""
    return [
        d for d in ALL_ROWS if all(d in s.scores for s in scored) and (only is None or d in only)
    ]


def thresholds(scored: list[Scored]) -> dict[str, float]:
    labels = [s.case.is_leaky for s in scored]
    return {
        d: threshold_at_fpr(labels, [s.scores[d] for s in scored], MAX_FPR) for d in rows_of(scored)
    }


def evaluate(scored: list[Scored], cut: dict[str, float]) -> dict:
    labels = [s.case.is_leaky for s in scored]
    report: dict = {"n_cases": len(scored), "n_leaky": sum(labels), "detectors": {}}
    rows = rows_of(scored, cut)
    for d in rows:
        scores = [s.scores[d] for s in scored]
        c = confusion(labels, scores, cut[d])
        by_type = {}
        for leak in LEAK_TYPES[1:]:
            group = [s for s in scored if s.case.leak_type == leak]
            if group:
                hits = sum(s.scores[d] >= cut[d] for s in group)
                by_type[leak] = round(hits / len(group), 4)
        groups = [s.case.template_id for s in scored]
        report["detectors"][d] = {
            **c.as_dict(),
            "ci95": bootstrap_ci(labels, scores, cut[d], groups),
            "threshold": None if cut[d] == float("inf") else cut[d],
            "auc": round(auc(roc_curve(labels, scores)), 4),
            "recall_by_type": by_type,
            "n_unverifiable": sum(d in s.verdicts for s in scored),
            "median_ms": round(
                1000 * sorted(s.seconds.get(d, 0) for s in scored)[len(scored) // 2], 2
            )
            if d in ("baseline", "static", "replay")
            else None,
        }
    if any(s.v1 is not None for s in scored):
        report["v1_reading"] = v1_reading(scored, cut)
    detected = [s for s in scored if s.case.is_leaky and s.scores["combined"] >= cut["combined"]]
    typed = [s for s in detected if s.case.leak_type in s.leak_types]
    report["attribution_accuracy"] = round(len(typed) / len(detected), 4) if detected else None
    errors = [
        {
            "case_id": s.case.case_id,
            "truth": s.case.leak_type,
            "predicted_types": s.leak_types,
            "combined_score": round(s.scores["combined"], 4),
            "sql": s.case.sql,
        }
        for s in scored
        if (s.scores["combined"] >= cut["combined"]) != s.case.is_leaky
    ]
    report["combined_errors"] = errors
    report["unverifiable_cases"] = [
        {
            "case_id": s.case.case_id,
            "truth": s.case.leak_type,
            "detector": d,
            "score": round(s.scores[d], 4),
            "reason": s.reasons[d],
        }
        for s in scored
        for d in rows
        if d in s.verdicts
    ]
    return report


def v1_reading(scored: list[Scored], cut: dict[str, float]) -> dict:
    """What row (a) could not read, by truth label, and what it would have said had it read L7.

    An unreadable clean case scores "not detected", a true negative that v1 did not earn; a leaky
    one is a miss. `raw` is the score v1 gave before that rule: for the L7 cases it is v1 judging
    them by its own six rules.
    """
    unreadable = [s for s in scored if s.v1 is not None and s.v1.unreadable]
    by_truth = Counter(s.case.leak_type for s in unreadable)
    result: dict = {
        "unreadable_by_truth": dict(sorted(by_truth.items())),
        "clean_credited_unread": by_truth["clean"],
        "unreadable_cases": [
            {"case_id": s.case.case_id, "truth": s.case.leak_type, "reason": s.v1.reasons["static"]}
            for s in unreadable
            if s.v1 is not None
        ],
    }
    leaky = [s for s in scored if s.case.is_leaky and s.v1 is not None]
    for row in V1_ROWS:
        if row not in cut or not leaky:
            continue
        which = row.removeprefix("v1_")
        raw = {
            s.case.case_id: (getattr(s.v1, f"raw_{which}") if s.v1.raw_static is not None else None)
            for s in leaky
        }
        forced = [s for s in leaky if raw[s.case.case_id] is not None]
        hits = sum(s.scores[row] >= cut[row] for s in leaky)
        hits_raw = sum(
            (raw[s.case.case_id] if raw[s.case.case_id] is not None else s.scores[row]) >= cut[row]
            for s in leaky
        )
        result[row] = {
            "recall": round(hits / len(leaky), 4),
            "recall_if_unnamed_types_were_read": round(hits_raw / len(leaky), 4),
            "forced_to_not_detected": len(forced),
            "of_which_flagged_by_v1_raw": sum(raw[s.case.case_id] >= cut[row] for s in forced),
        }
    return result


def v1_spec_for(spec_path: Path) -> LeakageSpec:
    """The spec of `spec_path` reduced to the fields the first rule set had (row (b))."""
    return LeakageSpec.from_dict(
        v1_spec_data(yaml.safe_load(spec_path.read_text(encoding="utf-8")))
    )


def score_v1(cases: list[LeakCase], spec_path: Path, warehouse: Path, seed: int) -> list[V1Score]:
    """Row (a): the v1 detector, in its own virtualenv, over the world in `warehouse`."""
    spec_data = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory(prefix="sqlguard-v1-") as work:
        return run_v1(
            [c.sql for c in cases],
            spec_data=spec_data,
            warehouse=world_dir(warehouse, seed),
            seed=seed,
            python=ensure_v1_python(),
            work_dir=Path(work),
        )


def score_split(
    split: str, seed: int, warehouse: Path, catalog: LakehouseCatalog, with_v1: bool
) -> list[Scored]:
    """Every row for one split, with the spec `splits.yaml` gives it."""
    spec_path = load_splits()[split].spec
    cases = generate_cases(seed, (split,))
    v1 = score_v1(cases, spec_path, warehouse, seed) if with_v1 else None
    return score_cases(
        cases,
        catalog,
        LeakageSpec.from_yaml(spec_path),
        seed,
        v1_spec=v1_spec_for(spec_path),
        v1_scores=v1,
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prereg_payload(seed: int, warehouse: Path, with_v1: bool = True) -> dict:
    """What `prereg` writes: thresholds fit on dev, and what existed when they were fit."""
    catalog = synthetic_world(warehouse, seed)
    dev = score_split("dev", seed, warehouse, catalog, with_v1)
    sha, _ = checkout_state()
    weights = Weights.load()
    manifest = load_splits()
    cut = thresholds(dev)
    return {
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "git_sha": sha,
        "seed": seed,
        "fit_splits": ["dev"],
        "seen_splits": sorted(seen_splits()),
        "specs": {
            name: {"file": split.spec.name, "sha256": _sha256(split.spec)}
            for name, split in manifest.items()
        },
        "spec_sha256": _sha256(SYNTHETIC_SPEC),
        # a split with no template file yet is the evidence that nothing was written before this
        "template_files": {
            name: len(list((TEMPLATES_DIR / name).glob("*.sql"))) for name in manifest
        },
        "v1": {"tag": V1_TAG, "commit": tag_commit()} if with_v1 else None,
        "frozen_files": frozen_hashes(splits=sorted(seen_splits())),
        "expectations": list(EXPECTATIONS),
        "max_fpr": MAX_FPR,
        # null = the detector cannot reach FPR <= max_fpr with any positive threshold
        "thresholds": {d: (None if t == float("inf") else t) for d, t in cut.items()},
        "weights": {"rules": weights.rules, "replay_mismatch": weights.replay_mismatch},
        "dev_summary": {
            d: {"recall": m["recall"], "fpr": m["fpr"]}
            for d, m in evaluate(dev, cut)["detectors"].items()
        },
    }


def prereg(seed: int, warehouse: Path, out_dir: Path = RESULTS_DIR, with_v1: bool = True) -> Path:
    sha, dirty = checkout_state()
    if dirty:
        raise SystemExit("commit your changes first: pre-registration must point at a clean SHA")
    payload = prereg_payload(seed, warehouse, with_v1)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"prereg-{sha}.json"
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def load_prereg(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as err:
        raise SystemExit(f"cannot read the pre-registration {path}: {err}") from err


def latest_prereg(out_dir: Path = RESULTS_DIR) -> dict:
    """The newest pre-registration by the time it records (never by file time). Only for runs on
    splits that were already seen: a split nobody has seen needs an explicit `--prereg`."""
    found = [load_prereg(p) for p in out_dir.glob("prereg-*.json")]
    if not found:
        raise SystemExit("no pre-registration found; run `sqlguard-bench leakage prereg` first")
    return max(found, key=lambda pre: pre.get("created_at", ""))


def require_frozen_run(
    held_out: list[str],
    pre: dict,
    prereg_path: Path | None,
    *,
    with_v1: bool,
    root: Path | None = None,
) -> None:
    """Refuse a run on a split nobody has seen unless everything it relies on is as it was frozen.

    It needs an explicit pre-registration; a git checkout (at `root`, whatever the working
    directory) with a clean tree; the commit the pre-registration was written at to be an ancestor
    of HEAD; the SHA-256 of every frozen file (all of `src/sqlguard`, the harness, the specs and
    manifest, the briefs, the templates of the seen splits) to match the record; and a threshold
    for every row it reports (row (a) only if `with_v1`).
    """
    names = ", ".join(held_out)
    if prereg_path is None:
        raise SystemExit(f"a run on unseen split(s) {names} needs an explicit --prereg PATH")
    checkout = root or ROOT
    _, dirty = checkout_state(checkout)
    if dirty:
        raise SystemExit("commit or stash your changes first: a held-out run needs a clean tree")
    if not pre.get("git_sha") or not is_ancestor(pre["git_sha"], checkout):
        raise SystemExit(
            f"{prereg_path.name} was written at {pre.get('git_sha')}, which is not an ancestor "
            "of HEAD: the checkout is not a continuation of the frozen commit"
        )
    recorded = pre.get("frozen_files")
    if not recorded:
        raise SystemExit(f"{prereg_path} records no frozen_files hashes; write it with `prereg`")
    problems = frozen_mismatches(recorded, checkout, pre.get("seen_splits", ()))
    if problems:
        shown = "; ".join(problems[:8])
        raise SystemExit(f"the frozen files changed since {prereg_path.name}: {shown}")
    rows = [*DETECTORS, *V1_SPEC_ROWS, *(V1_ROWS if with_v1 else ())]
    missing = [row for row in rows if row not in pre["thresholds"]]
    if missing:
        raise SystemExit(
            f"{prereg_path.name} has no threshold for {missing}; refusing to drop rows"
        )


def _real(seed: int, warehouse: Path, cut: dict[str, float]) -> dict | None:
    from bench.leakage import real_data

    templates = load_templates("real")
    if not templates:
        return None
    catalog = LakehouseCatalog.local(warehouse / "online-retail-ii")
    if not catalog.table_names():
        real_data.build_real_world(catalog)
    spec = LeakageSpec.from_yaml(real_data.SPEC_PATH)
    cases = [c for t in templates for c in render(t)]
    return evaluate(score_cases(cases, catalog, spec, seed), cut)


def run(
    seed: int,
    warehouse: Path,
    with_real: bool,
    out_dir: Path = RESULTS_DIR,
    splits: tuple[str, ...] = DEFAULT_SPLITS,
    *,
    with_v1: bool = True,
    with_fixes: bool = False,
    prereg_path: Path | None = None,
) -> Path:
    """Score the template `splits` with the pre-registered thresholds and write the report.

    Row (a) needs the v1 virtualenv and a pre-registration that has its thresholds; `with_v1=False`
    leaves it out. `with_fixes` adds the fix-suggestion rates by verification level. A split nobody
    has seen needs `prereg_path` and passes `require_frozen_run`; runs on seen splits take the
    newest pre-registration when none is given and leave out rows it has no threshold for.
    """
    unknown = [name for name in splits if not load_templates(name)]
    if not splits or unknown:
        raise SystemExit(f"no templates for split(s) {unknown or splits}; known: {SPLITS}")
    started = time.perf_counter()
    pre = load_prereg(prereg_path) if prereg_path else latest_prereg(out_dir)
    # seen-ness for the guard is what the pre-registration recorded, never the live manifest: a
    # split cannot be declared seen after the fact to skip the checks
    recorded_seen = set(pre["seen_splits"]) if "seen_splits" in pre else set(seen_splits())
    held_out = [name for name in splits if name not in recorded_seen]
    if held_out:
        require_frozen_run(held_out, pre, prereg_path, with_v1=with_v1)
    cut = {d: float("inf") if t is None else t for d, t in pre["thresholds"].items()}
    with_v1 = with_v1 and all(d in cut for d in V1_ROWS)
    catalog = synthetic_world(warehouse, seed)
    results = {}
    curves = {}
    fixes = {}
    for split in splits:
        scored = score_split(split, seed, warehouse, catalog, with_v1)
        results[split] = evaluate(scored, cut)
        labels = [s.case.is_leaky for s in scored]
        curves[split] = {d: roc_curve(labels, [s.scores[d] for s in scored]) for d in DETECTORS}
        if with_fixes:
            fixes[split] = _fix_rates(split, seed, catalog)
    sha, dirty = git_sha()
    result = {
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "git_sha": sha,
        "dirty": dirty,
        "seed": seed,
        "prereg_sha": pre["git_sha"],
        "prereg_file": str(prereg_path) if prereg_path else None,
        "held_out_run_checked": bool(held_out),
        "thresholds": pre["thresholds"],
        "splits": results,
        "in_sample_splits": [name for name in splits if name in seen_splits(pre)],
        "specs": {name: load_splits()[name].spec.name for name in splits},
        "optimism_gap": optimism.run(
            catalog, LeakageSpec.from_yaml(SYNTHETIC_SPEC), OPTIMISM_CUTOFF
        ),
        "real_data": _real(seed, warehouse, cut) if with_real else None,
        "fixes": fixes or None,
    }
    result["runtime_seconds"] = round(time.perf_counter() - started, 1)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{datetime.now(UTC):%Y%m%d}-{sha}"
    (out_dir / f"{stem}.json").write_text(json.dumps(result, indent=2, default=str) + "\n")
    (out_dir / f"{stem}.md").write_text(markdown(result), encoding="utf-8")
    plot_roc(curves[splits[-1]], out_dir / f"{stem}-roc.png")
    return out_dir / f"{stem}.json"


def _fix_rates(split: str, seed: int, catalog: LakehouseCatalog) -> dict:
    """Share of the split's leaky L1-L5 cases by the verification of their suggested fix."""
    from bench.leakage import fixes_eval

    spec = LeakageSpec.from_yaml(load_splits()[split].spec)
    return fixes_eval.evaluate(generate_cases(seed, (split,)), catalog, spec, seed)


def markdown(result: dict) -> str:
    in_sample = set(result["in_sample_splits"])
    seen = [name for name in result["splits"] if name in in_sample]
    fresh = [name for name in result["splits"] if name not in in_sample]
    lines = [
        f"# Leakage benchmark — {result['git_sha']} (prereg {result['prereg_sha']})",
        "",
        f"Thresholds were fit on dev at FPR ≤ {MAX_FPR:.0%} (prereg {result['prereg_sha']}). "
        "Intervals: 95% bootstrap over templates (a template's cases are resampled together).",
        "",
    ]
    if seen:
        lines += [
            f"**In-sample:** {', '.join(seen)} {'was' if len(seen) == 1 else 'were'} seen while "
            "the rules and the spec were developed, so these numbers track progress and say "
            "nothing about unseen queries.",
            "",
        ]
    if fresh:
        lines += [f"Not seen during development: {', '.join(fresh)}.", ""]
    if result.get("specs"):
        lines += [
            "Spec per split: " + ", ".join(f"{n} `{f}`" for n, f in result["specs"].items()),
            "",
        ]
    if any(d.startswith("v1_") for m in result["splits"].values() for d in m["detectors"]):
        lines += [
            "Rows: (a) v1 = the detector of tag `v0.1.1` in its own virtualenv; (b) v2 with a "
            "spec that has only the fields v1 knew; (c) v2 with the full spec. A query a row "
            'cannot read scores "not detected" and is listed below with the reason.',
            "",
        ]
    sections = dict(result["splits"])
    if result.get("real_data"):
        sections["real (Online Retail II)"] = result["real_data"]
    for name, split in sections.items():
        lines += [
            f"## {name} — {split['n_cases']} cases ({split['n_leaky']} leaky)",
            "",
            "| detector | recall [95% CI] | FPR [95% CI] | precision | F1 | AUC | median ms |",
            "|---|---|---|---|---|---|---|",
        ]
        for d, m in split["detectors"].items():
            ms = "" if m["median_ms"] is None else m["median_ms"]
            lines.append(
                f"| {ROW_NAMES.get(d, d)} | {m['recall']:.2f} {_ci(m, 'recall')} | "
                f"{m['fpr']:.2f} {_ci(m, 'fpr')} | "
                f"{m['precision']:.2f} | {m['f1']:.2f} | {m['auc']:.2f} | {ms} |"
            )
        reading = split.get("v1_reading")
        if reading:
            lines += ["", _v1_reading_lines(reading)]
        types = sorted({t for m in split["detectors"].values() for t in m["recall_by_type"]})
        lines += ["", "| recall by type | " + " | ".join(types) + " |"]
        lines.append("|---" * (len(types) + 1) + "|")
        for d, m in split["detectors"].items():
            row = " | ".join(f"{m['recall_by_type'].get(t, 0):.2f}" for t in types)
            lines.append(f"| {ROW_NAMES.get(d, d)} | {row} |")
        lines += ["", f"Attribution accuracy (combined): {split['attribution_accuracy']}", ""]
        counts = {
            d: m["n_unverifiable"] for d, m in split["detectors"].items() if m["n_unverifiable"]
        }
        if counts:
            lines += [
                "Unverifiable cases (the product fails closed on these): "
                + ", ".join(f"{ROW_NAMES.get(d, d)} {n}" for d, n in counts.items()),
                "",
            ]
            lines += [
                f"- `{u['case_id']}` ({u['truth']}) {ROW_NAMES.get(u['detector'], u['detector'])}, "
                f"score {u['score']}: "
                f"{u['reason']}"
                for u in split["unverifiable_cases"]
            ]
            lines.append("")
    for split, rates in (result.get("fixes") or {}).items():
        if split == next(iter(result["fixes"])):
            levels = list(rates["rates"])
            lines += [
                "## Fix suggestions by verification level",
                "",
                "Share of the leaky L1-L5 cases (L6 and L7 are explained, not rewritten) by the "
                "verification of the last rewrite.",
                "",
                "| split | cases | suggested | " + " | ".join(levels) + " |",
                "|---|---|---|" + "---|" * len(levels),
            ]
        shares = " | ".join(f"{rates['rates'][lv]:.3f} ({rates['counts'][lv]})" for lv in levels)
        lines.append(
            f"| {split} | {rates['n_cases']} | {rates['suggestion_rate']:.3f} | {shares} |"
        )
    if result.get("fixes"):
        lines.append("")
    gap = result["optimism_gap"]
    lines += [
        "## Optimism gap (logistic regression, synthetic world)",
        "",
        "| feature query | offline AUC | AUC at serving time | gap |",
        "|---|---|---|---|",
    ]
    for name, g in gap.items():
        lines.append(f"| {name} | {g['offline_auc']:.3f} | {g['real_auc']:.3f} | {g['gap']:+.3f} |")
    return "\n".join(lines) + "\n"


def _v1_reading_lines(reading: dict) -> str:
    unread = ", ".join(f"{t} {n}" for t, n in reading["unreadable_by_truth"].items()) or "none"
    lines = [
        f"Row (a) could not read: {unread} (by truth label). The {reading['clean_credited_unread']}"
        " clean ones score as true negatives v1 did not earn."
    ]
    for row in V1_ROWS:
        if row in reading:
            r = reading[row]
            lines.append(
                f"{ROW_NAMES[row]}: recall {r['recall']:.2f}; had v1 been scored on the "
                f"{r['forced_to_not_detected']} cases of a type it has no name for by its own "
                f"rules, {r['recall_if_unnamed_types_were_read']:.2f} "
                f"({r['of_which_flagged_by_v1_raw']} of them flagged)."
            )
    return "\n\n".join(lines)


def _ci(metrics: dict, key: str) -> str:
    lo, hi = metrics.get("ci95", {}).get(key, (None, None))
    return "" if lo is None or lo != lo else f"[{lo:.2f}, {hi:.2f}]"


def plot_roc(curves: dict[str, list], path: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    # Validated categorical slots (light surface); dashes add a non-color encoding.
    styles = {
        "baseline": ("#2a78d6", (0, (2, 2))),
        "static": ("#eb6834", (0, (6, 2))),
        "replay": ("#1baf7a", (0, (1, 1.5))),
        "combined": ("#eda100", "solid"),
    }
    fig, ax = plt.subplots(figsize=(5, 5), dpi=150)
    fig.patch.set_facecolor("#fcfcfb")
    ax.set_facecolor("#fcfcfb")
    for name, points in curves.items():
        xy = sorted({(f, t) for _, f, t in points} | {(1.0, 1.0)})
        color, dash = styles[name]
        ax.plot(
            [p[0] for p in xy],
            [p[1] for p in xy],
            color=color,
            linestyle=dash,
            linewidth=2,
            label=f"{name} (AUC {auc(points):.2f})",
        )
    ax.axvline(MAX_FPR, color="#52514e", linewidth=0.8, alpha=0.4)
    ax.set_xlabel("False positive rate", color="#52514e")
    ax.set_ylabel("Recall", color="#52514e")
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1.02)
    ax.grid(color="#52514e", alpha=0.1)
    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.tick_params(colors="#52514e")
    ax.legend(frameon=False, loc="lower right")
    fig.tight_layout()
    fig.savefig(path, facecolor=fig.get_facecolor())
    plt.close(fig)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m bench.leakage.run", description=__doc__)
    parser.add_argument("action", choices=["prereg", "run"])
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--warehouse", type=Path, default=Path(".warehouse/bench"))
    parser.add_argument("--out-dir", type=Path, default=RESULTS_DIR)
    parser.add_argument("--real", action="store_true", help="also run on Online Retail II")
    parser.add_argument(
        "--splits",
        nargs="+",
        default=list(DEFAULT_SPLITS),
        choices=SPLITS,
        help="template splits to score (default: dev dev_v1b)",
    )
    parser.add_argument(
        "--prereg",
        type=Path,
        help="the pre-registration to use (required for a split nobody has seen)",
    )
    parser.add_argument(
        "--skip-v1", action="store_true", help="leave out row (a), the v1 detector (no virtualenv)"
    )
    parser.add_argument(
        "--fixes", action="store_true", help="also report fix rates by verification level"
    )
    args = parser.parse_args(argv)
    if args.action == "prereg":
        path = prereg(args.seed, args.warehouse, args.out_dir, not args.skip_v1)
        print(f"pre-registration written to {path}")
    else:
        path = run(
            args.seed,
            args.warehouse,
            args.real,
            args.out_dir,
            tuple(args.splits),
            with_v1=not args.skip_v1,
            with_fixes=args.fixes,
            prereg_path=args.prereg,
        )
        print(f"results written to {path}")


if __name__ == "__main__":
    main()
