"""Synthetic Riiid-shaped interaction log (train.csv) with many multi-question bundles.

Questions of one task container share a timestamp, as in the real data. Usage:
python generate.py OUT_DIR
"""

import csv
import random
import sys
from pathlib import Path

rng = random.Random(42)
out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)

rows = []
row_id = 0
for user_id in rng.sample(range(10_000, 99_999_999), 40):
    skill = rng.uniform(0.35, 0.9)
    ts = 0
    for container in range(rng.randint(12, 35)):
        if container > 0:
            ts += rng.randint(15_000, 900_000)
        if container > 2 and rng.random() < 0.1:
            rows.append([row_id, ts, user_id, rng.randint(1, 32_000), 1, container, -1, -1])
            row_id += 1
            continue
        # a bundle is answered together; its questions tend to share difficulty
        p = min(0.97, max(0.03, skill + rng.uniform(-0.3, 0.3)))
        for _ in range(rng.choice([1, 2, 3, 3, 4, 5])):
            correct = 1 if rng.random() < p else 0
            rows.append(
                [
                    row_id,
                    ts,
                    user_id,
                    rng.randint(0, 13_522),
                    0,
                    container,
                    rng.randint(0, 3),
                    correct,
                ]
            )
            row_id += 1

with (out / "train.csv").open("w", newline="") as f:
    w = csv.writer(f)
    w.writerow(
        [
            "row_id",
            "timestamp",
            "user_id",
            "content_id",
            "content_type_id",
            "task_container_id",
            "user_answer",
            "answered_correctly",
        ]
    )
    w.writerows(rows)
