"""Synthetic TalkingData-shaped click log (train.csv). Usage: python generate.py OUT_DIR

Bursty fraud-like groups click many times in a short span and rarely download; a downloader's
clicks are followed by few further clicks, so look-ahead counts carry label signal. click_id
is added to train (the real train has none; test does) so every spine row has a key.
"""

import csv
import datetime as dt
import random
import sys
from pathlib import Path

rng = random.Random(42)
out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)

START = dt.datetime(2017, 11, 7, 0, 0, 0)
SPAN = 2 * 86400
rows = []
for _ in range(220):
    ip = rng.randint(1, 365_000)
    app, device, os_, channel = (
        rng.randint(1, 30),
        rng.choice([1, 1, 2, 3060]),
        rng.randint(1, 40),
        rng.randint(100, 500),
    )
    bursty = rng.random() < 0.3
    n = rng.randint(8, 30) if bursty else rng.randint(1, 6)
    t0 = rng.randrange(SPAN)
    gap = 40 if bursty else 3600
    t = t0
    for _ in range(n):
        t += rng.randint(0, gap)
        click = START + dt.timedelta(seconds=min(t, SPAN - 1))
        p = 0.002 if bursty else 0.12
        attributed = 1 if rng.random() < p else 0
        att_time = ""
        if attributed:
            att_time = (click + dt.timedelta(seconds=rng.randint(5, 20_000))).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
        rows.append(
            [
                ip,
                app if rng.random() < 0.8 else rng.randint(1, 30),
                device,
                os_,
                channel,
                click.strftime("%Y-%m-%d %H:%M:%S"),
                att_time,
                attributed,
            ]
        )

rows.sort(key=lambda r: r[5])
with (out / "train.csv").open("w", newline="") as f:
    w = csv.writer(f)
    w.writerow(
        [
            "click_id",
            "ip",
            "app",
            "device",
            "os",
            "channel",
            "click_time",
            "attributed_time",
            "is_attributed",
        ]
    )
    for i, r in enumerate(rows):
        w.writerow([i, *r])
