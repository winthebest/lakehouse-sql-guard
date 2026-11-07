"""Synthetic web-store page-view log in the shape the KDD Cup 2000 account describes.

One row per page view; session_length (pages in the whole visit) is written onto every row of
the session after it ends; leaves_site is 1 on the last page. Usage: python generate.py OUT_DIR
"""

import csv
import datetime as dt
import random
import sys
from pathlib import Path

rng = random.Random(42)
out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)

TEMPLATES = [
    "main/home",
    "main/departments",
    "products/detail",
    "products/search",
    "cart/view",
    "checkout/billing",
    "main/assortment",
    "main/legwear",
]
START = dt.datetime(2000, 1, 30, 0, 0, 0)
rows = []
for session_id in range(1, 401):
    n = max(1, int(rng.expovariate(1 / 5)) + 1)
    t = START + dt.timedelta(seconds=rng.randrange(60 * 86400))
    pages = []
    for seq in range(1, n + 1):
        pages.append([session_id, seq, t.strftime("%Y-%m-%d %H:%M:%S"), rng.choice(TEMPLATES)])
        t += dt.timedelta(seconds=rng.randint(3, 600))
    for i, p in enumerate(pages):
        rows.append([*p, n, 1 if i == n - 1 else 0])

with (out / "page_views.csv").open("w", newline="") as f:
    w = csv.writer(f)
    w.writerow(
        [
            "session_id",
            "request_seq",
            "request_time",
            "page_template",
            "session_length",
            "leaves_site",
        ]
    )
    w.writerows(rows)
