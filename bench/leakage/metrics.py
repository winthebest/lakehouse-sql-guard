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
