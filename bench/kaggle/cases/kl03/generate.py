"""Synthetic INFORMS-2010-shaped 5-minute series (training.csv). Usage: python generate.py OUT_DIR

Two price series stand in for the 609 explanatory variables. TargetVariable is 1 when the
first series' last price is higher 60 minutes later, which mirrors the host's description
(variables at t, target built from t+60 minutes).
"""

import csv
import datetime as dt
import math
import random
import sys
from pathlib import Path

rng = random.Random(42)
out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)

times = []
day = dt.date(2008, 3, 3)
while len(times) < 20 * 78:
    if day.weekday() < 5:
        t = dt.datetime.combine(day, dt.time(9, 30))
        for _ in range(78):
            times.append(t)
            t += dt.timedelta(minutes=5)
    day += dt.timedelta(days=1)


def series(start):
    p, bars = start, []
    for _ in times:
        o = p
        p *= math.exp(rng.gauss(0, 0.002))
        hi = max(o, p) * (1 + abs(rng.gauss(0, 0.0007)))
        lo = min(o, p) * (1 - abs(rng.gauss(0, 0.0007)))
        bars.append((round(o, 4), round(hi, 4), round(lo, 4), round(p, 4)))
    return bars


a, b = series(42.0), series(118.0)
with (out / "training.csv").open("w", newline="") as f:
    w = csv.writer(f)
    w.writerow(
        [
            "Timestamp",
            "TargetVariable",
            "Variable74OPEN",
            "Variable74HIGH",
            "Variable74LOW",
            "Variable74LAST_PRICE",
            "Variable142OPEN",
            "Variable142HIGH",
            "Variable142LOW",
            "Variable142LAST_PRICE",
        ]
    )
    for i, t in enumerate(times):
        j = i + 12
        y = (1 if a[j][3] > a[i][3] else 0) if j < len(times) else ""
        w.writerow([t.strftime("%Y-%m-%d %H:%M:%S"), y, *a[i], *b[i]])
