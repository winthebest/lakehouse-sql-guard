"""Synthetic Riiid-shaped interaction log (train.csv). Usage: python generate.py OUT_DIR"""

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
    n_containers = rng.randint(15, 45)
    for container in range(n_containers):
        if container > 0:
            ts += rng.randint(15_000, 900_000)
        if container > 2 and rng.random() < 0.1:
            rows.append([row_id, ts, user_id, rng.randint(1, 32_000), 1, container, -1, -1])
            row_id += 1
            continue
        for _ in range(rng.choice([1, 1, 1, 2, 3, 4])):
            correct = 1 if rng.random() < skill else 0
            answer = rng.randint(0, 3)
            rows.append(
                [row_id, ts, user_id, rng.randint(0, 13_522), 0, container, answer, correct]
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
