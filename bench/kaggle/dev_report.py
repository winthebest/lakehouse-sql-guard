"""Extra numbers for the development runs: MCC, Youden J, AUPRC of the replay score, precision at
deployment prevalence, one-sided Clopper-Pearson bounds, and the pairs a checker tells apart.

    uv run python -m bench.kaggle.dev_report KAGGLE.csv [--out report.json]
    uv run python -m bench.kaggle.dev_report --holdout RESULT.json [--out report.json]

Everything here is computed from files a run already wrote, with the functions of
`bench.leakage.metrics`. These are development numbers (the rules were changed while looking at
the data); the bounds are for comparison with held-out runs, not evidence. The authors of the
cases and the judges are Claude models; no human rated anything.
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from bench.leakage import metrics
from bench.leakage.metrics import Confusion


def two_layer(conf: Confusion, level: float = 0.95) -> dict[str, Any]:
    """MCC, J, precision at each deployment prevalence and the one-sided bounds of a confusion."""
    positives, negatives = conf.tp + conf.fn, conf.tn + conf.fp
    tpr = conf.recall
    fpr = conf.fpr
    return {
        "tp": conf.tp,
        "fp": conf.fp,
        "tn": conf.tn,
        "fn": conf.fn,
        "recall": round(tpr, 4),
        "fpr": round(fpr, 4),
        "mcc": round(metrics.mcc(conf), 4),
        "youden_j": round(metrics.youden_j(conf), 4),
        "precision_at_prevalence": {
            f"{int(pi * 100)}%": round(metrics.precision_at_prevalence(tpr, fpr, pi), 4)
            for pi in metrics.DEPLOYMENT_PREVALENCES
        },
        "fpr_upper_95_one_sided": _bound(conf.fp, negatives, level, "upper"),
        "recall_lower_95_one_sided": _bound(conf.tp, positives, level, "lower"),
    }


def _bound(k: int, n: int, level: float, side: str) -> float | None:
    value = metrics.clopper_pearson_one_sided(k, n, level, side)  # type: ignore[arg-type]
    return None if value != value else round(value, 4)


def kaggle_report(rows: list[dict[str, str]]) -> dict[str, Any]:
    """Metrics of a Kaggle result table (`<stem>.csv`): per mode, the verdict-based confusion, the
    AUPRC of the replay score (the mismatch rate), and the pairs told apart."""
    report: dict[str, Any] = {}
    for mode in ("static", "replay"):
        mine = [r for r in rows if r["mode"] == mode]
        labels = [r["label"] == "leaky" for r in mine]
        flagged = [r["class"] == "leaky" for r in mine]
        conf = metrics.confusion(labels, [float(f) for f in flagged], 1.0)
        entry = two_layer(conf)
        if mode == "replay":
            scores = [_replay_score(r) for r in mine]
            entry["auprc_replay_score"] = round(metrics.auprc(labels, scores), 4)
        report[mode] = entry
    report["pairs"] = pairs_told_apart(rows)
    return report


def _replay_score(row: dict[str, str]) -> float:
    compared = int(row.get("rows_compared") or 0)
    if row.get("replay_status") == "mismatch" and compared:
        return int(row["rows_mismatched"]) / compared
    return 0.0


def pairs_told_apart(rows: list[dict[str, str]]) -> dict[str, Any]:
    """A pair is told apart when its leaky query is flagged and its clean twin is not (static).

    A case's queries are `leaky_k` and `clean_k` for the same k (the file names carry the pair)."""
    flagged = {(r["case"], r["file"]): r["class"] == "leaky" for r in rows if r["mode"] == "static"}
    by_pair: dict[tuple[str, str], dict[str, bool]] = defaultdict(dict)
    for (case, name), is_flagged in flagged.items():
        kind, _, number = name.removesuffix(".sql").partition("_")
        by_pair[(case, number)][kind] = is_flagged
    total = told = 0
    missed = []
    for key, sides in sorted(by_pair.items()):
        if {"leaky", "clean"} <= sides.keys():
            total += 1
            if sides["leaky"] and not sides["clean"]:
                told += 1
            else:
                missed.append(f"{key[0]}/{key[1]}")
    return {"told_apart": told, "pairs": total, "not_told_apart": missed}


def holdout_report(result: dict[str, Any], split: str = "holdout_v3") -> dict[str, Any]:
    """Metrics of the combined, static and replay detectors from a leakage run's result JSON."""
    detectors = result["splits"][split]["detectors"]
    report = {}
    for name in ("static", "replay", "combined"):
        det = detectors[name]
        conf = Confusion(det["tp"], det["fp"], det["tn"], det["fn"])
        entry = two_layer(conf)
        template = det.get("template_fpr")
        if template:
            entry["template_fpr"] = {
                "k": template["k"],
                "n": template["n"],
                "upper_95_one_sided": _bound(template["k"], template["n"], 0.95, "upper"),
            }
        report[name] = entry
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("csv", nargs="?", type=Path, help="a Kaggle result table")
    parser.add_argument("--holdout", type=Path, help="a leakage run result JSON")
    parser.add_argument("--split", default="holdout_v3")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    if bool(args.csv) == bool(args.holdout):
        parser.error("give a Kaggle table or --holdout, not both")
    if args.csv:
        with open(args.csv, newline="", encoding="utf-8") as handle:
            report = {
                "dev": True,
                "source": args.csv.name,
                **kaggle_report(list(csv.DictReader(handle))),
            }
    else:
        result = json.loads(args.holdout.read_text(encoding="utf-8"))
        report = {
            "dev": True,
            "source": args.holdout.name,
            "split": args.split,
            **holdout_report(result, args.split),
        }
    text = json.dumps(report, indent=2) + "\n"
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
