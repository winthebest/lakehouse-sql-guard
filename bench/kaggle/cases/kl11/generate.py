"""Synthetic LendingClub-shaped accepted-loans table. Usage: python generate.py OUT_DIR

Only finished loans (Fully Paid / Charged Off), as both blogs keep. Payment, recovery,
last-payment, credit-pull and settlement fields are filled the way they would be after the
loan has run, so they mirror the outcome.
"""

import csv
import datetime as dt
import random
import sys
from pathlib import Path

rng = random.Random(42)
out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)


def add_months(d, n):
    y, m = divmod(d.month - 1 + n, 12)
    return dt.date(d.year + y, m + 1, 1)


rows = []
for i in range(1000):
    issue = add_months(dt.date(2013, 1, 1), rng.randrange(60))
    term = rng.choice([36, 36, 60])
    amount = rng.randrange(1000, 35000, 25)
    grade = rng.choice("AABBBCCDDEFG")
    rate = {"A": 7, "B": 11, "C": 14, "D": 18, "E": 21, "F": 25, "G": 28}[grade] + rng.uniform(
        -1, 1
    )
    r = rate / 1200
    installment = amount * r / (1 - (1 + r) ** -term)
    income = round(rng.lognormvariate(11, 0.5), -2)
    dti = round(rng.uniform(0, 40), 2)
    earliest = add_months(issue, -rng.randint(36, 360))
    p_default = 0.05 + 0.03 * "ABCDEFG".index(grade) + 0.002 * dti
    charged_off = rng.random() < p_default
    if charged_off:
        months_paid = rng.randint(1, term - 1)
        total_pymnt = installment * months_paid
        prncp = total_pymnt * rng.uniform(0.5, 0.8)
        recoveries = round(rng.uniform(0, 0.2) * (amount - prncp), 2)
        settle = "Y" if rng.random() < 0.15 else "N"
        status = "Charged Off"
    else:
        months_paid = term if rng.random() < 0.6 else rng.randint(6, term)
        total_pymnt = installment * term * (1 if months_paid == term else rng.uniform(0.8, 1.0))
        prncp, recoveries, settle, status = amount, 0.0, "N", "Fully Paid"
    last_pymnt = add_months(issue, months_paid)
    last_pull = add_months(last_pymnt, rng.randint(0, 24))
    rows.append(
        [
            1_000_000 + i,
            amount,
            f" {term} months",
            round(rate, 2),
            round(installment, 2),
            grade,
            income,
            dti,
            issue.isoformat(),
            earliest.isoformat(),
            status,
            round(total_pymnt, 2),
            round(prncp, 2),
            recoveries,
            last_pymnt.isoformat(),
            round(installment if months_paid else 0, 2),
            last_pull.isoformat(),
            settle,
        ]
    )

with (out / "accepted_2007_to_2018q4.csv").open("w", newline="") as f:
    w = csv.writer(f)
    w.writerow(
        [
            "id",
            "loan_amnt",
            "term",
            "int_rate",
            "installment",
            "grade",
            "annual_inc",
            "dti",
            "issue_d",
            "earliest_cr_line",
            "loan_status",
            "total_pymnt",
            "total_rec_prncp",
            "recoveries",
            "last_pymnt_d",
            "last_pymnt_amnt",
            "last_credit_pull_d",
            "debt_settlement_flag",
        ]
    )
    w.writerows(rows)
