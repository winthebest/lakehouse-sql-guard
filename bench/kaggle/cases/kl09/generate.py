"""Synthetic Favorita-shaped daily sales (train.csv). Usage: python generate.py OUT_DIR

Follows the forum account: an item is put on promotion after a few weak days, so future
promotion flags say something about past sales. As on the data page, store/item/days with
zero sales have no row, and some onpromotion values are missing.
"""

import csv
import datetime as dt
import random
import sys
from pathlib import Path

rng = random.Random(42)
out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)

START = dt.date(2017, 3, 1)
DAYS = 120
rows = []
for store_nbr in [1, 3, 8, 24, 44]:
    for item_nbr in rng.sample(range(96_995, 2_127_114), 8):
        base = rng.uniform(2, 25)
        recent = []
        promo_left = 0
        for d in range(DAYS):
            day = START + dt.timedelta(days=d)
            weak = len(recent) >= 3 and sum(recent[-3:]) / 3 < 0.7 * base
            if promo_left == 0 and weak and rng.random() < 0.6:
                promo_left = rng.randint(3, 7)
            promo = promo_left > 0
            promo_left = max(0, promo_left - 1)
            sales = max(0.0, rng.gauss(base * (1.6 if promo else 1.0), base * 0.45))
            sales = round(sales, 3) if item_nbr % 3 == 0 else float(round(sales))
            recent.append(sales)
            if sales == 0:
                continue
            flag = "" if rng.random() < 0.05 else ("True" if promo else "False")
            rows.append([day.isoformat(), store_nbr, item_nbr, sales, flag])

rows.sort(key=lambda r: (r[0], r[1], r[2]))
with (out / "train.csv").open("w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["id", "date", "store_nbr", "item_nbr", "unit_sales", "onpromotion"])
    for i, r in enumerate(rows):
        w.writerow([i, *r])
