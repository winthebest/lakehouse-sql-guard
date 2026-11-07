"""Synthetic IEEE-CIS-shaped transactions (train_transaction.csv). Usage: python generate.py OUT_DIR

Each card has a first-use day; D1 counts days since then, so day(TransactionDT) - D1 is
constant per card, which is what participants used to rebuild a card/user id. Following the
host's labelling rule, once a card has a reported fraud its later transactions are fraud too.
"""

import csv
import random
import sys
from pathlib import Path

rng = random.Random(42)
out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)

REF = 86_400  # TransactionDT starts one day after the hidden reference
SPAN_DAYS = 182
rows = []
for _ in range(160):
    card1 = rng.randint(1000, 18396)
    card2 = float(rng.randint(100, 600))
    addr1 = float(rng.randint(100, 540))
    email = rng.choice(["gmail.com", "yahoo.com", "hotmail.com", "anonymous.com", "aol.com"])
    product = rng.choice(["W", "W", "W", "C", "R", "H", "S"])
    first_day = rng.randint(-200, SPAN_DAYS - 10)
    amount_scale = rng.uniform(20, 300)
    fraud_from = None
    if rng.random() < 0.2:
        fraud_from = rng.randint(max(0, first_day), SPAN_DAYS)
    n = rng.randint(1, 18)
    days = sorted(rng.randint(max(0, first_day), SPAN_DAYS) for _ in range(n))
    for day in days:
        dt_sec = REF + day * 86_400 + rng.randint(0, 86_399)
        is_fraud = 1 if fraud_from is not None and day >= fraud_from else 0
        amount = round(rng.lognormvariate(0, 0.6) * amount_scale * (2 if is_fraud else 1), 3)
        rows.append(
            [
                is_fraud,
                dt_sec,
                amount,
                product,
                card1,
                card2,
                addr1,
                email,
                float(day - first_day) if day >= first_day else 0.0,
            ]
        )

rows.sort(key=lambda r: r[1])
with (out / "train_transaction.csv").open("w", newline="") as f:
    w = csv.writer(f)
    w.writerow(
        [
            "TransactionID",
            "isFraud",
            "TransactionDT",
            "TransactionAmt",
            "ProductCD",
            "card1",
            "card2",
            "addr1",
            "P_emaildomain",
            "D1",
        ]
    )
    for i, r in enumerate(rows):
        w.writerow([2_987_000 + i, *r])
