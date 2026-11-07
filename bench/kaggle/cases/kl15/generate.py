"""Synthetic Red-Hat-Business-Value-shaped people and activity files.

Usage: python generate.py OUT_DIR

Outcomes are piecewise constant per group_1 over time, which is the regularity the forum
describes (same group, same date, same outcome).
"""

import csv
import datetime as dt
import random
import sys
from pathlib import Path

rng = random.Random(42)
out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)

START = dt.date(2022, 7, 17)
DAYS = 400
people, acts = [], []
act_no = 0
for g in rng.sample(range(1, 51_000), 50):
    switches = sorted(rng.sample(range(1, DAYS), rng.randint(0, 3)))
    state0 = rng.random() < 0.45

    def group_state(d, switches=switches, state0=state0):
        return state0 ^ (sum(1 for s in switches if s <= d) % 2 == 1)

    for _ in range(rng.randint(2, 8)):
        pid = f"ppl_{rng.randint(1, 399_999)}"
        joined = rng.randrange(DAYS // 2)
        people.append(
            [
                pid,
                f"group {g}",
                (START + dt.timedelta(days=joined)).isoformat(),
                rng.randint(0, 100),
            ]
        )
        days = sorted(rng.randint(joined, DAYS - 1) for _ in range(rng.randint(1, 12)))
        for d in days:
            act_no += 1
            outcome = group_state(d)
            if rng.random() < 0.03:
                outcome = not outcome
            cat = rng.choice(["type 1", "type 2", "type 2", "type 3", "type 4", "type 5"])
            acts.append(
                [
                    pid,
                    f"act2_{act_no}",
                    (START + dt.timedelta(days=d)).isoformat(),
                    cat,
                    f"type {rng.randint(1, 3000)}",
                    int(outcome),
                ]
            )

seen = set()
people = [p for p in people if not (p[0] in seen or seen.add(p[0]))]
pids = {p[0] for p in people}
acts = [a for a in acts if a[0] in pids]
with (out / "people.csv").open("w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["people_id", "group_1", "date", "char_38"])
    w.writerows(people)
with (out / "act_train.csv").open("w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["people_id", "activity_id", "date", "activity_category", "char_10", "outcome"])
    w.writerows(sorted(acts, key=lambda a: (a[2], a[1])))
