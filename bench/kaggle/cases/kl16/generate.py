"""Synthetic Kobe-Bryant-Shot-Selection-shaped shot table (data.csv).

Usage: python generate.py OUT_DIR

One player's shots over several seasons; accuracy drifts over time and by zone, so
all-history zone accuracy differs from accuracy over earlier games.
"""

import csv
import datetime as dt
import random
import sys
from pathlib import Path

rng = random.Random(42)
out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)

ZONES = {
    "Center(C)": (0, 0.47),
    "Left Side(L)": (-150, 0.40),
    "Right Side(R)": (150, 0.41),
    "Left Side Center(LC)": (-100, 0.36),
    "Right Side Center(RC)": (100, 0.37),
    "Back Court(BC)": (0, 0.02),
}
TYPES = {
    "Jump Shot": 0.0,
    "Layup": 0.12,
    "Dunk": 0.45,
    "Tip Shot": -0.05,
    "Hook Shot": 0.03,
    "Bank Shot": 0.05,
}
OPP = ["POR", "SAS", "UTA", "PHX", "HOU", "DEN", "SAC", "BOS", "NYK", "MIA"]
rows = []
shot_id = 0
game_id = 20000000
day = dt.date(2000, 10, 31)
for season_no in range(4):
    season = f"{2000 + season_no}-{(1 + season_no) % 100:02d}"
    form = rng.uniform(-0.05, 0.05)
    for _ in range(30):
        day += dt.timedelta(days=rng.randint(1, 4))
        game_id += 1
        opponent = rng.choice(OPP)
        event = 0
        for _ in range(rng.randint(8, 20)):
            event += rng.randint(2, 30)
            zone = rng.choice(list(ZONES))
            stype = rng.choice(list(TYPES))
            p = (
                ZONES[zone][1]
                + TYPES[stype]
                + form
                + 0.04 * season_no * (1 if zone == "Center(C)" else -1)
            )
            made = 1.0 if rng.random() < max(0.01, min(0.95, p)) else 0.0
            shot_id += 1
            rows.append(
                [
                    shot_id,
                    game_id,
                    event,
                    day.isoformat(),
                    season,
                    rng.randint(1, 4),
                    rng.randint(0, 11),
                    rng.randint(0, 59),
                    stype,
                    rng.randint(0, 30),
                    zone,
                    opponent,
                    made,
                ]
            )

with (out / "data.csv").open("w", newline="") as f:
    w = csv.writer(f)
    w.writerow(
        [
            "shot_id",
            "game_id",
            "game_event_id",
            "game_date",
            "season",
            "period",
            "minutes_remaining",
            "seconds_remaining",
            "combined_shot_type",
            "shot_distance",
            "shot_zone_area",
            "opponent",
            "shot_made_flag",
        ]
    )
    w.writerows(rows)
