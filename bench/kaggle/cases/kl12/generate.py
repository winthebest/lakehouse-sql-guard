"""Synthetic BTS on-time-performance-shaped flights table (flights.csv).

Usage: python generate.py OUT_DIR

Each aircraft (Tail_Number) flies 3-5 legs a day; a late inbound leg delays the next one.
Times are local HHMM integers as in BTS; crs_dep_ts combines FlightDate and CRSDepTime so
the prediction time is one column. Legs never cross midnight here.
"""

import csv
import datetime as dt
import random
import sys
from pathlib import Path

rng = random.Random(42)
out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)

AIRPORTS = ["ATL", "BOS", "DEN", "DFW", "JFK", "LAX", "MIA", "ORD", "SEA", "SFO"]
AIRLINES = ["AA", "DL", "UA", "WN", "B6"]


def hhmm(minutes):
    return (minutes // 60) * 100 + minutes % 60


rows = []
flight_no = 100
for k in range(40):
    tail = f"N{rng.randint(100, 999)}{rng.choice('ABCDEFGHJKLMNPRSTUVWXYZ')}{k:02d}"
    airline = rng.choice(AIRLINES)
    for d in range(30):
        day = dt.date(2018, 1, 2) + dt.timedelta(days=d)
        where = rng.choice(AIRPORTS)
        sched = rng.randint(6 * 60, 8 * 60)
        prev_arr_actual = None
        for _ in range(rng.randint(3, 5)):
            dest = rng.choice([a for a in AIRPORTS if a != where])
            block = rng.randint(70, 240)
            if sched + block + 30 > 23 * 60 + 50:
                break
            own = max(
                -8, int(rng.gauss(0, 9)) + (rng.randint(20, 120) if rng.random() < 0.08 else 0)
            )
            late_aircraft = max(0, (prev_arr_actual or 0) + 35 - sched) if prev_arr_actual else 0
            dep_delay = max(own, late_aircraft)
            arr_delay = dep_delay + int(rng.gauss(0, 7))
            dep_actual = sched + dep_delay
            arr_actual = sched + block + arr_delay
            if arr_actual > 23 * 60 + 59 or dep_actual > 23 * 60 + 59:
                break
            causes = ["", "", "", "", ""]
            if arr_delay >= 15:
                late = min(late_aircraft, arr_delay)
                rest = arr_delay - late
                carrier = rest // 2
                weather = rest // 4 if rng.random() < 0.3 else 0
                nas = rest - carrier - weather
                causes = [carrier, weather, nas, 0, late]
            rows.append(
                [
                    2018,
                    1,
                    day.day,
                    day.isoweekday(),
                    day.isoformat(),
                    airline,
                    tail,
                    flight_no,
                    where,
                    dest,
                    hhmm(sched),
                    hhmm(dep_actual),
                    dep_delay,
                    hhmm(sched + block),
                    hhmm(arr_actual),
                    arr_delay,
                    1 if arr_delay >= 15 else 0,
                    block - 25 + int(rng.gauss(0, 5)),
                    rng.randint(200, 2500),
                    *causes,
                    f"{day.isoformat()} {sched // 60:02d}:{sched % 60:02d}:00",
                ]
            )
            flight_no += 1
            prev_arr_actual = arr_actual
            where = dest
            sched = sched + block + rng.randint(35, 90)

with (out / "flights.csv").open("w", newline="") as f:
    w = csv.writer(f)
    w.writerow(
        [
            "Year",
            "Month",
            "DayofMonth",
            "DayOfWeek",
            "FlightDate",
            "Reporting_Airline",
            "Tail_Number",
            "Flight_Number_Reporting_Airline",
            "Origin",
            "Dest",
            "CRSDepTime",
            "DepTime",
            "DepDelay",
            "CRSArrTime",
            "ArrTime",
            "ArrDelay",
            "ArrDel15",
            "AirTime",
            "Distance",
            "CarrierDelay",
            "WeatherDelay",
            "NASDelay",
            "SecurityDelay",
            "LateAircraftDelay",
            "crs_dep_ts",
        ]
    )
    w.writerows(rows)
