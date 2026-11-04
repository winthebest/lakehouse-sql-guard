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

Protocol: `prereg` picks each detector's threshold on the dev split (FPR <= 5%) and writes it
with the commit SHA *before* held-out templates exist; `run` evaluates with those thresholds.
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from bench.leakage import optimism
from bench.leakage.baseline import baseline_score
from bench.leakage.cases import LEAK_TYPES, LeakCase, generate_cases, load_templates, render
from bench.leakage.metrics import auc, bootstrap_ci, confusion, roc_curve, threshold_at_fpr
from bench.leakage.world import WorldConfig, build_world
from sqlguard.core.catalog import LakehouseCatalog
from sqlguard.leakage.replay import Replayer, ReplayStatus
from sqlguard.leakage.spec import LeakageSpec
from sqlguard.leakage.validator import UNCHECKED, LeakageReport, Weights, validate

DETECTORS = ("baseline", "static", "replay", "combined")
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


def git_sha() -> tuple[str, bool]:
    def out(*args: str) -> str:
        return subprocess.run(args, capture_output=True, text=True, check=False).stdout.strip()

    sha = out("git", "rev-parse", "--short", "HEAD") or "unknown"
    dirty = bool(out("git", "status", "--porcelain", "--untracked-files=no"))
    return sha, dirty


def unverifiable_reason(report: LeakageReport) -> str:
    """Why a validator report is `unverifiable` (empty for any other verdict)."""
    if report.verdict != "unverifiable":
        return ""
    unchecked = [f.message for f in report.findings if f.rule_id in UNCHECKED]
    if unchecked:
        return "; ".join(unchecked)
    if report.replay is not None and report.replay.reason:
        return report.replay.reason
    return "a static hint (L4) can only be settled by replay"


def score_cases(
    cases: list[LeakCase], catalog: LakehouseCatalog, spec: LeakageSpec, seed: int
) -> list[Scored]:
    schema = catalog.schema_for()
    replayer = Replayer(catalog, spec, max_points=24, seed=seed)
    current = replayer.current()
    weights = Weights.load()
    scored = []
    for case in cases:
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
        reasons = {
            "static": unverifiable_reason(static),
            "combined": unverifiable_reason(combined),
            "replay": replay.reason if replay.status is ReplayStatus.UNVERIFIABLE else "",
        }
        reasons = {d: r for d, r in reasons.items() if r}
        scored.append(
            Scored(
                case,
                {
                    "baseline": base,
                    "static": static.score,
                    "replay": replay_score,
                    "combined": combined.score,
                },
                combined.leak_types,
                seconds,
                {d: "unverifiable" for d in reasons},
                reasons,
            )
        )
    return scored


def thresholds(scored: list[Scored]) -> dict[str, float]:
    labels = [s.case.is_leaky for s in scored]
    return {d: threshold_at_fpr(labels, [s.scores[d] for s in scored], MAX_FPR) for d in DETECTORS}


def evaluate(scored: list[Scored], cut: dict[str, float]) -> dict:
    labels = [s.case.is_leaky for s in scored]
    report: dict = {"n_cases": len(scored), "n_leaky": sum(labels), "detectors": {}}
    for d in DETECTORS:
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
        for d in DETECTORS
        if d in s.verdicts
    ]
    return report


def synthetic_world(root: Path, seed: int) -> LakehouseCatalog:
    catalog = LakehouseCatalog.local(root / f"synthetic-{seed}")
    if not catalog.table_names():
        build_world(catalog, WorldConfig(seed=seed))
    return catalog


def prereg(seed: int, warehouse: Path, out_dir: Path = RESULTS_DIR) -> Path:
    spec = LeakageSpec.from_yaml(SYNTHETIC_SPEC)
    catalog = synthetic_world(warehouse, seed)
    dev = score_cases(generate_cases(seed, ("dev",)), catalog, spec, seed)
    sha, dirty = git_sha()
    if dirty:
        raise SystemExit("commit your changes first: pre-registration must point at a clean SHA")
    weights = Weights.load()
    payload = {
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "git_sha": sha,
        "seed": seed,
        "max_fpr": MAX_FPR,
        # null = the detector cannot reach FPR <= max_fpr with any positive threshold
        "thresholds": {d: (None if t == float("inf") else t) for d, t in thresholds(dev).items()},
        "weights": {"rules": weights.rules, "replay_mismatch": weights.replay_mismatch},
        "dev_summary": {
            d: {"recall": m["recall"], "fpr": m["fpr"]}
            for d, m in evaluate(dev, thresholds(dev))["detectors"].items()
        },
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"prereg-{sha}.json"
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def latest_prereg(out_dir: Path = RESULTS_DIR) -> dict:
    files = sorted(out_dir.glob("prereg-*.json"), key=lambda p: p.stat().st_mtime)
    if not files:
        raise SystemExit("no pre-registration found; run `sqlguard-bench leakage prereg` first")
    return json.loads(files[-1].read_text(encoding="utf-8"))


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


def run(seed: int, warehouse: Path, with_real: bool, out_dir: Path = RESULTS_DIR) -> Path:
    started = time.perf_counter()
    pre = latest_prereg(out_dir)
    cut = {d: float("inf") if t is None else t for d, t in pre["thresholds"].items()}
    spec = LeakageSpec.from_yaml(SYNTHETIC_SPEC)
    catalog = synthetic_world(warehouse, seed)
    splits = {}
    curves = {}
    for split in ("dev", "holdout"):
        cases = generate_cases(seed, (split,))
        if not cases:
            continue
        scored = score_cases(cases, catalog, spec, seed)
        splits[split] = evaluate(scored, cut)
        labels = [s.case.is_leaky for s in scored]
        curves[split] = {d: roc_curve(labels, [s.scores[d] for s in scored]) for d in DETECTORS}
    sha, dirty = git_sha()
    result = {
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "git_sha": sha,
        "dirty": dirty,
        "seed": seed,
        "prereg_sha": pre["git_sha"],
        "thresholds": pre["thresholds"],
        "splits": splits,
        "optimism_gap": optimism.run(catalog, spec, OPTIMISM_CUTOFF),
        "real_data": _real(seed, warehouse, cut) if with_real else None,
    }
    result["runtime_seconds"] = round(time.perf_counter() - started, 1)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{datetime.now(UTC):%Y%m%d}-{sha}"
    (out_dir / f"{stem}.json").write_text(json.dumps(result, indent=2, default=str) + "\n")
    (out_dir / f"{stem}.md").write_text(markdown(result), encoding="utf-8")
    plot_roc(curves.get("holdout") or curves["dev"], out_dir / f"{stem}-roc.png")
    return out_dir / f"{stem}.json"


def markdown(result: dict) -> str:
    lines = [
        f"# Leakage benchmark — {result['git_sha']} (prereg {result['prereg_sha']})",
        "",
        f"Thresholds chosen on dev at FPR ≤ {MAX_FPR:.0%} before held-out templates existed. "
        "Intervals: 95% bootstrap over templates (a template's cases are resampled together).",
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
                f"| {d} | {m['recall']:.2f} {_ci(m, 'recall')} | {m['fpr']:.2f} {_ci(m, 'fpr')} | "
                f"{m['precision']:.2f} | {m['f1']:.2f} | {m['auc']:.2f} | {ms} |"
            )
        types = sorted({t for m in split["detectors"].values() for t in m["recall_by_type"]})
        lines += ["", "| recall by type | " + " | ".join(types) + " |"]
        lines.append("|---" * (len(types) + 1) + "|")
        for d, m in split["detectors"].items():
            row = " | ".join(f"{m['recall_by_type'].get(t, 0):.2f}" for t in types)
            lines.append(f"| {d} | {row} |")
        lines += ["", f"Attribution accuracy (combined): {split['attribution_accuracy']}", ""]
        counts = {
            d: m["n_unverifiable"] for d, m in split["detectors"].items() if m["n_unverifiable"]
        }
        if counts:
            lines += [
                "Unverifiable cases (the product fails closed on these): "
                + ", ".join(f"{d} {n}" for d, n in counts.items()),
                "",
            ]
            lines += [
                f"- `{u['case_id']}` ({u['truth']}) {u['detector']}, score {u['score']}: "
                f"{u['reason']}"
                for u in split["unverifiable_cases"]
            ]
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
