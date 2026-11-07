"""Synthetic GA-Customer-Revenue-shaped visit log (train.csv). Usage: python generate.py OUT_DIR

One row per visit, as in the competition files, with the totals JSON flattened to the one
field used here. Buyers come back more often, so later visits carry signal about revenue.
"""

import csv
import datetime as dt
import random
import sys
from pathlib import Path

rng = random.Random(42)
out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)

START = 1_470_009_600  # 2016-08-01 00:00:00 UTC
SPAN = 120 * 86400
rows = []
for _ in range(300):
    visitor = str(rng.randint(10**18, 10**19 - 1))
    buyer = rng.random() < 0.25
    n = rng.choice([1, 1, 1, 2, 2, 3, 4, 6, 9]) + (3 if buyer else 0)
    starts = sorted(START + rng.randrange(SPAN) for _ in range(n))
    channel = rng.choice(["Organic Search", "Direct", "Referral", "Social", "Paid Search"])
    for number, t in enumerate(starts, 1):
        revenue = ""
        if buyer and rng.random() < 0.35:
            revenue = rng.randint(5, 400) * 1_000_000
        day = dt.datetime.fromtimestamp(t, dt.UTC).strftime("%Y%m%d")
        rows.append([channel, int(day), visitor, t, number, t, revenue])

rows.sort(key=lambda r: r[3])
with (out / "train.csv").open("w", newline="") as f:
    w = csv.writer(f)
    w.writerow(
        [
            "channelGrouping",
            "date",
            "fullVisitorId",
            "visitId",
            "visitNumber",
            "visitStartTime",
            "totals_transactionRevenue",
        ]
    )
    w.writerows(rows)
