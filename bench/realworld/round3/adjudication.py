"""The three labels of the third dbt search and the finding metrics registered for them.

Two blind reviewers label every finding (and every blind-reader line) `true`, `true_not_actionable`
or `false`, or `unsure` when they differ. Agreement between them is reported with kappa and
Gwet's AC1 (`bench/realworld/agreement.py`); this module turns the final labels into the figures
the registration names.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable

from bench.leakage.metrics import clopper_pearson_one_sided

LABELS = ("true", "true_not_actionable", "false", "unsure")
H2_PRECISION_FLOOR = 0.30  # lower bound of strict precision the registration asks for


def final_label(first: str, second: str) -> str:
    """The label both reviewers gave, or `unsure` when they differ."""
    for label in (first, second):
        if label not in LABELS:
            raise ValueError(f"unknown label {label!r}: use {LABELS}")
    return first if first == second else "unsure"


def finding_metrics(finals: Iterable[str]) -> dict:
    """Strict and loose precision, effective false positives and the goal check, per finding.

    - strict precision = `true` / findings (`unsure` counts against);
    - loose precision = (`true` + `true_not_actionable`) / findings;
    - effective false positive rate = (`false` + `true_not_actionable` + `unsure`) / findings: the
      share a developer would not act on;
    - `meets_goal`: the one-sided 95% lower bound of strict precision is at least 0.30.
    """
    finals = list(finals)
    unknown = set(finals) - set(LABELS)
    if unknown:
        raise ValueError(f"unknown label(s) {sorted(unknown)}: use {LABELS}")
    counts = Counter(finals)
    n = len(finals)
    true, not_actionable = counts["true"], counts["true_not_actionable"]
    lower = clopper_pearson_one_sided(true, n, side="lower") if n else None
    return {
        "findings": n,
        "counts": {label: counts[label] for label in LABELS},
        "precision_strict": true / n if n else None,
        "precision_strict_lower95": lower,
        "precision_loose": (true + not_actionable) / n if n else None,
        "effective_fp": (n - true) / n if n else None,
        "meets_goal": lower is not None and lower >= H2_PRECISION_FLOOR,
    }
