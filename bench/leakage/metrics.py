"""Detection metrics computed from (is_leaky, score) pairs, no third-party dependencies."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Confusion:
    tp: int
    fp: int
    tn: int
    fn: int

    @property
    def recall(self) -> float:
        return self.tp / (self.tp + self.fn) if self.tp + self.fn else 0.0

    @property
    def fpr(self) -> float:
        return self.fp / (self.fp + self.tn) if self.fp + self.tn else 0.0

    @property
    def precision(self) -> float:
        return self.tp / (self.tp + self.fp) if self.tp + self.fp else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if p + r else 0.0

    def as_dict(self) -> dict[str, float]:
        return {
            "tp": self.tp,
            "fp": self.fp,
            "tn": self.tn,
            "fn": self.fn,
            "recall": round(self.recall, 4),
            "fpr": round(self.fpr, 4),
            "precision": round(self.precision, 4),
            "f1": round(self.f1, 4),
        }


def confusion(labels: list[bool], scores: list[float], threshold: float) -> Confusion:
    """Predict leaky when score >= threshold."""
    tp = fp = tn = fn = 0
    for leaky, score in zip(labels, scores, strict=True):
        predicted = score >= threshold
        if predicted and leaky:
            tp += 1
        elif predicted:
            fp += 1
        elif leaky:
            fn += 1
        else:
            tn += 1
    return Confusion(tp, fp, tn, fn)


def roc_curve(labels: list[bool], scores: list[float]) -> list[tuple[float, float, float]]:
    """(threshold, fpr, tpr) points from the strictest threshold to the loosest."""
    thresholds = sorted(set(scores), reverse=True)
    points = [(float("inf"), 0.0, 0.0)]
    for t in thresholds:
        c = confusion(labels, scores, t)
        points.append((t, c.fpr, c.recall))
    return points


def auc(points: list[tuple[float, float, float]]) -> float:
    """Trapezoidal area under (fpr, tpr), closing the curve at (1, 1)."""
    xy = sorted({(fpr, tpr) for _, fpr, tpr in points} | {(1.0, 1.0)})
    return sum((x1 - x0) * (y0 + y1) / 2 for (x0, y0), (x1, y1) in zip(xy, xy[1:], strict=False))


def threshold_at_fpr(labels: list[bool], scores: list[float], max_fpr: float = 0.05) -> float:
    """Highest-recall threshold whose FPR stays within `max_fpr`, placed with a margin.

    The cut sits halfway between the lowest admissible score and the next lower score, so
    it classifies dev exactly as the boundary score would, without hugging the weakest
    leaky case seen on dev (a held-out case scoring slightly lower is still caught).
    """
    points = roc_curve(labels, scores)
    best = None
    for i, (_, fpr, _) in enumerate(points):
        if fpr <= max_fpr:
            best = i
    if best is None or best == 0:
        return float("inf")
    if best + 1 < len(points):
        return (points[best][0] + points[best + 1][0]) / 2
    return points[best][0]


def bootstrap_ci(
    labels: list[bool],
    scores: list[float],
    threshold: float,
    groups: list[str],
    *,
    n_resamples: int = 2000,
    seed: int = 0,
    level: float = 0.95,
) -> dict[str, tuple[float, float]]:
    """Percentile intervals for recall and FPR at a fixed threshold.

    Cases rendered from the same template share their structure, so whole templates are
    resampled (cluster bootstrap) instead of single cases; resampling cases would
    understate the uncertainty. A resample without leaky (or clean) cases does not
    contribute to the recall (or FPR) interval.
    """
    import random

    by_group: dict[str, list[int]] = {}
    for i, g in enumerate(groups):
        by_group.setdefault(g, []).append(i)
    clusters = list(by_group.values())
    rng = random.Random(seed)
    recalls: list[float] = []
    fprs: list[float] = []
    for _ in range(n_resamples):
        idx = [i for _ in clusters for i in rng.choice(clusters)]
        c = confusion([labels[i] for i in idx], [scores[i] for i in idx], threshold)
        if c.tp + c.fn:
            recalls.append(c.recall)
        if c.fp + c.tn:
            fprs.append(c.fpr)

    def interval(values: list[float]) -> tuple[float, float]:
        if not values:
            return (float("nan"), float("nan"))
        values = sorted(values)
        lo = values[int((1 - level) / 2 * (len(values) - 1))]
        hi = values[int((1 + level) / 2 * (len(values) - 1))]
        return (round(lo, 4), round(hi, 4))

    return {"recall": interval(recalls), "fpr": interval(fprs)}


def _binomial_tail(k: int, n: int, p: float, upper: bool) -> float:
    """P(X >= k) when `upper`, else P(X <= k), for X ~ Binomial(n, p)."""
    from math import comb

    terms = range(k, n + 1) if upper else range(0, k + 1)
    return sum(comb(n, i) * p**i * (1 - p) ** (n - i) for i in terms)


def clopper_pearson(k: int, n: int, level: float = 0.95) -> tuple[float, float]:
    """Exact two-sided interval for a proportion of `k` in `n` (the rate cannot be pinned down
    more tightly than this by that many independent units; zero of 29 is still up to 0.12)."""
    if n <= 0:
        return (float("nan"), float("nan"))
    tail = (1 - level) / 2

    def solve(target: float, k_: int, upper: bool) -> float:
        low, high = 0.0, 1.0
        for _ in range(60):
            mid = (low + high) / 2
            above = _binomial_tail(k_, n, mid, upper) > target
            # both tails grow with p when `upper`, and shrink with p otherwise
            if above == upper:
                high = mid
            else:
                low = mid
        return (low + high) / 2

    lower = 0.0 if k == 0 else solve(tail, k, upper=True)
    upper_bound = 1.0 if k == n else solve(tail, k, upper=False)
    return (round(lower, 4), round(upper_bound, 4))


def template_rates(
    labels: list[bool],
    scores: list[float],
    threshold: float,
    groups: list[str],
    unverifiable: list[bool] | None = None,
) -> dict:
    """Template-level false positive rate and block rate on the clean templates, with exact
    intervals.

    A clean template is the unit of independence (its cases are renderings of one query), so the
    false positive rate counts templates: one flagged if any of its cases scores at or above the
    threshold. The block rate also counts a case the detector could not check (the product fails
    closed on it), so a false positive cannot hide as "unverifiable".
    """
    flagged: dict[str, bool] = {}
    blocked: dict[str, bool] = {}
    cases = hit = stopped = 0
    for leaky, score, group, unchecked in zip(
        labels, scores, groups, unverifiable or [False] * len(labels), strict=True
    ):
        if leaky:
            continue
        cases += 1
        is_flagged = score >= threshold
        hit += is_flagged
        stopped += is_flagged or unchecked
        flagged[group] = flagged.get(group, False) or is_flagged
        blocked[group] = blocked.get(group, False) or is_flagged or unchecked
    n = len(flagged)

    def entry(k: int) -> dict:
        lo, hi = clopper_pearson(k, n)
        return {"k": k, "n": n, "rate": round(k / n, 4) if n else 0.0, "ci95": (lo, hi)}

    return {
        "template_fpr": entry(sum(flagged.values())),
        "block_rate_clean": entry(sum(blocked.values())),
        "clean_cases": cases,
        "clean_cases_flagged": hit,
        "clean_cases_blocked": stopped,
    }
