"""Synthetic Airbnb-New-User-shaped users table (train_users.csv). Usage: python generate.py OUT_DIR

date_first_booking is set only for users who booked (country_destination != NDF), as the
data page and thread 17595 describe. timestamp_first_active keeps the real YYYYMMDDhhmmss
number format and can precede account creation.
"""

import csv
import datetime as dt
import random
import string
import sys
from pathlib import Path

rng = random.Random(42)
out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)

COUNTRIES = (
    ["US"] * 30
    + ["FR"] * 3
    + ["IT"] * 2
    + ["GB", "ES", "CA", "DE", "NL", "AU", "PT"]
    + ["other"] * 5
)
START = dt.date(2012, 1, 1)
rows = []
for _ in range(900):
    uid = "".join(rng.choice(string.ascii_lowercase + string.digits) for _ in range(10))
    created = START + dt.timedelta(days=rng.randrange(900))
    active = dt.datetime.combine(created, dt.time()) - dt.timedelta(
        seconds=rng.choice([rng.randint(0, 3600), rng.randint(0, 86400 * 400)])
    )
    active += (
        dt.timedelta(seconds=rng.randint(0, 86399)) if active.date() < created else dt.timedelta(0)
    )
    booked = rng.random() < 0.42
    country = rng.choice(COUNTRIES) if booked else "NDF"
    booking = ""
    if booked:
        booking = (created + dt.timedelta(days=min(365, int(rng.expovariate(1 / 40))))).isoformat()
    gender = rng.choice(["-unknown-", "FEMALE", "MALE", "OTHER"])
    age = "" if rng.random() < 0.4 else rng.randint(18, 80)
    rows.append(
        [
            uid,
            created.isoformat(),
            active.strftime("%Y%m%d%H%M%S"),
            booking,
            gender,
            age,
            rng.choice(["basic", "facebook", "google"]),
            rng.choice([0, 0, 3, 12, 25]),
            rng.choice(["en"] * 9 + ["fr", "de", "zh"]),
            rng.choice(["direct", "sem-brand", "sem-non-brand", "seo", "other"]),
            rng.choice(["Web", "Moweb", "iOS", "Android"]),
            rng.choice(["Mac Desktop", "Windows Desktop", "iPhone", "iPad", "Other/Unknown"]),
            country,
        ]
    )

with (out / "train_users.csv").open("w", newline="") as f:
    w = csv.writer(f)
    w.writerow(
        [
            "id",
            "date_account_created",
            "timestamp_first_active",
            "date_first_booking",
            "gender",
            "age",
            "signup_method",
            "signup_flow",
            "language",
            "affiliate_channel",
            "signup_app",
            "first_device_type",
            "country_destination",
        ]
    )
    w.writerows(rows)
