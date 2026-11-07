"""Agreement between two raters and the alert-burden numbers of a real-world adjudication.

All raters and judges of the findings are Claude models; no human rated anything. The numbers
describe how consistently two such readers judge, not how a person would.

Labels are the strings of the `controller` and `reviewer` columns of `findings.csv`; a pair counts
only when both are filled. `cohen_kappa` and `gwet_ac1` take the two label lists. Gwet's AC1 is
reported beside kappa because kappa collapses when one label dominates (24 of 33 round-2 findings
were judged false), which is the case here.
"""

from __future__ import annotations

import csv
from collections import Counter
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

TRUE_LABELS = ("true", "true_actionable")
NOT_ACTIONABLE = "true_not_actionable"
FALSE_LABEL = "false"
UNSURE = "unsure"


def _pairs(a: Sequence[str], b: Sequence[str]) -> list[tuple[str, str]]:
    if len(a) != len(b):
        raise ValueError(f"two raters must label the same items: {len(a)} against {len(b)}")
    return [(x, y) for x, y in zip(a, b, strict=True)]


def observed_agreement(a: Sequence[str], b: Sequence[str]) -> float:
    pairs = _pairs(a, b)
    if not pairs:
        raise ValueError("no items to compare")
    return sum(x == y for x, y in pairs) / len(pairs)


def cohen_kappa(a: Sequence[str], b: Sequence[str]) -> float | None:
    """Cohen's kappa; None when chance agreement is 1 (both raters used one label only)."""
    pairs = _pairs(a, b)
    n = len(pairs)
    if not n:
        raise ValueError("no items to compare")
    po = observed_agreement(a, b)
    left, right = Counter(x for x, _ in pairs), Counter(y for _, y in pairs)
    pe = sum(left[label] * right[label] for label in set(left) | set(right)) / (n * n)
    return None if pe == 1 else (po - pe) / (1 - pe)


def gwet_ac1(a: Sequence[str], b: Sequence[str]) -> float | None:
    """Gwet's AC1 for two raters: (po - pe) / (1 - pe) with pe = sum(pi_k (1 - pi_k)) / (K - 1),
    pi_k the mean share of label k over both raters. None with fewer than two labels in use."""
    pairs = _pairs(a, b)
    n = len(pairs)
    if not n:
        raise ValueError("no items to compare")
    po = observed_agreement(a, b)
    labels = {x for pair in pairs for x in pair}
    if len(labels) < 2:
        return None
    share = Counter(label for pair in pairs for label in pair)
    pe = sum((share[k] / (2 * n)) * (1 - share[k] / (2 * n)) for k in labels) / (len(labels) - 1)
    return None if pe == 1 else (po - pe) / (1 - pe)


def agreement(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Agreement of the `controller` and `reviewer` columns over the rows where both are filled."""
    both = [
        (str(r["controller"]).strip().lower(), str(r["reviewer"]).strip().lower())
        for r in rows
        if str(r.get("controller") or "").strip() and str(r.get("reviewer") or "").strip()
    ]
    if not both:
        return {"n": 0, "observed": None, "kappa": None, "gwet_ac1": None}
    a, b = [x for x, _ in both], [y for _, y in both]
    return {
        "n": len(both),
        "observed": round(observed_agreement(a, b), 4),
        "kappa": None if (k := cohen_kappa(a, b)) is None else round(k, 4),
        "gwet_ac1": None if (g := gwet_ac1(a, b)) is None else round(g, 4),
    }


def read_csv(path: Path | str) -> list[dict[str, str]]:
    with open(path, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def burden(findings: Sequence[dict[str, Any]], models: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Precision and alert-burden numbers from adjudicated findings and the checked models.

    Findings are the scored ones only (demoted INFO rows are not in `findings.csv`). A finding's
    label is its `final` column. Strict precision counts `unsure` and `true_not_actionable` as not
    true; loose precision counts `unsure` as true. The effective false-positive rate is
    (false + true-but-not-actionable) / findings. Verdict coverage is the share of checked training
    models whose verdict is leaky or clean (not unverifiable or error).
    """
    labels = Counter(str(r.get("final") or "").strip().lower() for r in findings)
    n = len(findings)
    true = sum(labels[k] for k in TRUE_LABELS)
    unsure, false = labels[UNSURE], labels[FALSE_LABEL]
    not_actionable = labels[NOT_ACTIONABLE]
    unlabelled = n - true - unsure - false - not_actionable
    decided = [m for m in models if str(m.get("verdict") or "") in ("leaky", "clean")]
    return {
        "findings": n,
        "true": true,
        "false": false,
        "unsure": unsure,
        "true_not_actionable": not_actionable,
        "unlabelled": unlabelled,
        "precision_strict": round(true / n, 4) if n else None,
        "precision_loose": round((true + unsure) / n, 4) if n else None,
        "effective_false_positive": round((false + not_actionable) / n, 4) if n else None,
        "models": len(models),
        "models_with_verdict": len(decided),
        "verdict_coverage": round(len(decided) / len(models), 4) if models else None,
        "findings_per_model": round(n / len(models), 3) if models else None,
    }
