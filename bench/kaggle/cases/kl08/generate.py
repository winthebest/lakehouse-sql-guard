"""Synthetic G-Research-shaped 1-minute bars (train.csv). Usage: python generate.py OUT_DIR

Target follows the participant's description: the log return between the closes 1 and 16
minutes ahead, so it needs prices that arrive 15+ minutes after the row. The host's
residualisation against the market is left out. A few minutes are missing, as in the real file.
"""

import csv
import math
import random
import sys
from pathlib import Path

rng = random.Random(42)
out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)

START = 1_623_542_400  # 2021-06-13 00:00:00 UTC
MINUTES = 600
rows = []
for asset_id, price in [(1, 35_000.0), (6, 2_500.0), (9, 160.0)]:
    closes = {}
    bars = []
    for m in range(MINUTES):
        ts = START + 60 * m
        open_ = price
        price *= math.exp(rng.gauss(0, 0.0015))
        high = max(open_, price) * (1 + abs(rng.gauss(0, 0.0005)))
        low = min(open_, price) * (1 - abs(rng.gauss(0, 0.0005)))
        closes[ts] = price
        if rng.random() < 0.02:
            continue  # missing minute
        count = rng.randint(5, 400)
        volume = rng.uniform(0.5, 50.0)
        vwap = (high + low + price) / 3
        bars.append([ts, asset_id, count, open_, high, low, price, volume, vwap])
    for b in bars:
        ts = b[0]
        c1, c16 = closes.get(ts + 60), closes.get(ts + 960)
        b.append(round(math.log(c16 / c1), 8) if c1 and c16 else None)
        rows.append(b)

rows.sort(key=lambda r: (r[0], r[1]))
with (out / "train.csv").open("w", newline="") as f:
    w = csv.writer(f)
    w.writerow(
        [
            "timestamp",
            "Asset_ID",
            "Count",
            "Open",
            "High",
            "Low",
            "Close",
            "Volume",
            "VWAP",
            "Target",
        ]
    )
    for r in rows:
        w.writerow([("" if v is None else (round(v, 6) if isinstance(v, float) else v)) for v in r])
