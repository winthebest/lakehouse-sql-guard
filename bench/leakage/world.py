"""Synthetic churn world written to Iceberg as a real, multi-snapshot history.

Timeline semantics (all timestamps naive UTC):
- `events` arrive in nightly batches: an event on day d is normally ingested at midnight d+1;
  a fraction arrives late (extra 1-7 days). One Iceberg commit per ingestion night.
- `user_stats` is recomputed every midnight from events ingested so far; one commit per night.
- `entities` is static and committed before the world starts.
- `labels` (the prediction spine) and `user_outcomes` (facts observed after churn) are
  committed at the end, so they were never visible at prediction time.

Each commit stores its logical time in the snapshot summary (`LOGICAL_TS_PROPERTY`), which
lets point-in-time replay map a prediction timestamp to the snapshot that was visible then.
"""

from __future__ import annotations

import json
import math
import random
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path

import pyarrow as pa

from sqlguard.core.catalog import LOGICAL_TS_PROPERTY, LakehouseCatalog

START = datetime(2026, 1, 1)
COUNTRIES = ["VN", "US", "DE", "JP", "BR"]

SCHEMAS = {
    "entities": pa.schema(
        [("user_id", pa.int64()), ("country", pa.string()), ("signup_ts", pa.timestamp("us"))]
    ),
    "events": pa.schema(
        [
            ("event_id", pa.int64()),
            ("user_id", pa.int64()),
            ("event_ts", pa.timestamp("us")),
            ("ingested_at", pa.timestamp("us")),
            ("amount", pa.float64()),
        ]
    ),
    "user_stats": pa.schema(
        [
            ("user_id", pa.int64()),
            ("updated_at", pa.timestamp("us")),
            ("total_spend", pa.float64()),
            ("n_orders", pa.int64()),
        ]
    ),
    "labels": pa.schema(
        [("user_id", pa.int64()), ("label_ts", pa.timestamp("us")), ("churned", pa.int64())]
    ),
    "user_outcomes": pa.schema(
        [
            ("user_id", pa.int64()),
            ("label_ts", pa.timestamp("us")),
            ("refund_after_churn", pa.float64()),
            ("days_to_churn", pa.int64()),
        ]
    ),
}


@dataclass(frozen=True)
class WorldConfig:
    seed: int = 42
    n_users: int = 300
    n_days: int = 45
    late_rate: float = 0.10
    churn_rate: float = 0.4
    label_start_day: int = 14
    label_end_day: int = 30
    midnight_label_rate: float = 0.3
    horizon_days: int = 14


@dataclass(frozen=True)
class WorldSummary:
    n_events: int
    n_late_events: int
    n_labels: int
    churn_rate: float
    snapshots: dict[str, int]

    @property
    def late_rate(self) -> float:
        return self.n_late_events / self.n_events if self.n_events else 0.0


def _poisson(rng: random.Random, lam: float) -> int:
    """Knuth's algorithm; fine for the small rates used here."""
    limit, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= limit:
            return k
        k += 1


def _midnight(day: int) -> datetime:
    return START + timedelta(days=day)


def _commit(catalog: LakehouseCatalog, table: str, rows: list[dict], logical: datetime) -> None:
    if not rows:
        return
    data = pa.Table.from_pylist(rows, schema=SCHEMAS[table])
    catalog.table(table).append(
        data, snapshot_properties={LOGICAL_TS_PROPERTY: logical.isoformat()}
    )


def build_world(catalog: LakehouseCatalog, config: WorldConfig | None = None) -> WorldSummary:
    """Generate the world deterministically from `config.seed` and write it to `catalog`."""
    cfg = config or WorldConfig()
    rng = random.Random(cfg.seed)
    for name, schema in SCHEMAS.items():
        catalog.create_empty(name, schema)

    users = range(1, cfg.n_users + 1)
    rates = {u: rng.uniform(0.2, 2.0) for u in users}
    churn_day = {
        u: rng.randint(cfg.label_start_day + 1, cfg.n_days - 1)
        if rng.random() < cfg.churn_rate
        else None
        for u in users
    }
    entities = [
        {
            "user_id": u,
            "country": rng.choice(COUNTRIES),
            "signup_ts": START - timedelta(days=rng.randint(1, 365)),
        }
        for u in users
    ]
    _commit(catalog, "entities", entities, START)

    # Events grouped by the night they are ingested.
    by_ingest_day: dict[int, list[dict]] = defaultdict(list)
    n_events = n_late = 0
    for day in range(cfg.n_days):
        for u in users:
            if churn_day[u] is not None and day >= churn_day[u]:
                continue
            for _ in range(_poisson(rng, rates[u])):
                late = rng.random() < cfg.late_rate
                ingest_day = day + 1 + (rng.randint(1, 7) if late else 0)
                n_events += 1
                if ingest_day > cfg.n_days:
                    continue  # still in flight when the world ends
                n_late += late
                by_ingest_day[ingest_day].append(
                    {
                        "event_id": n_events,
                        "user_id": u,
                        "event_ts": _midnight(day) + timedelta(seconds=rng.randint(0, 86_399)),
                        "ingested_at": _midnight(ingest_day),
                        "amount": round(rng.lognormvariate(3.0, 0.8), 2),
                    }
                )

    spend: dict[int, float] = defaultdict(float)
    orders: dict[int, int] = defaultdict(int)
    for night in range(1, cfg.n_days + 1):
        batch = by_ingest_day.get(night, [])
        _commit(catalog, "events", batch, _midnight(night))
        for row in batch:
            spend[row["user_id"]] += row["amount"]
            orders[row["user_id"]] += 1
        stats = [
            {
                "user_id": u,
                "updated_at": _midnight(night),
                "total_spend": round(spend[u], 2),
                "n_orders": orders[u],
            }
            for u in sorted(orders)
        ]
        _commit(catalog, "user_stats", stats, _midnight(night))

    labels, outcomes = [], []
    for u in users:
        days = rng.sample(range(cfg.label_start_day, cfg.label_end_day + 1), k=rng.choice([1, 2]))
        for day in sorted(days):
            if churn_day[u] is not None and day >= churn_day[u]:
                continue  # already churned: no prediction is made
            offset = 0 if rng.random() < cfg.midnight_label_rate else rng.randint(1, 86_399)
            label_ts = _midnight(day) + timedelta(seconds=offset)
            churn_at = _midnight(churn_day[u]) if churn_day[u] is not None else None
            churned = int(
                churn_at is not None
                and label_ts < churn_at <= label_ts + timedelta(days=cfg.horizon_days)
            )
            labels.append({"user_id": u, "label_ts": label_ts, "churned": churned})
            if churned:
                outcomes.append(
                    {
                        "user_id": u,
                        "label_ts": label_ts,
                        "refund_after_churn": round(rng.uniform(0, 50), 2),
                        "days_to_churn": (churn_at - label_ts).days,
                    }
                )
    world_end = _midnight(cfg.n_days + cfg.horizon_days)
    _commit(catalog, "labels", labels, world_end)
    _commit(catalog, "user_outcomes", outcomes, world_end)

    return WorldSummary(
        n_events=sum(len(rows) for rows in by_ingest_day.values()),
        n_late_events=n_late,
        n_labels=len(labels),
        churn_rate=sum(r["churned"] for r in labels) / len(labels),
        snapshots={name: len(catalog.snapshots(name)) for name in SCHEMAS},
    )


def write_timeline(catalog: LakehouseCatalog, path: Path) -> None:
    """Persist `{table: [{snapshot_id, logical_ts}]}` for inspection and replay debugging."""
    timeline = {
        name: [
            {"snapshot_id": s.snapshot_id, "logical_ts": s.visible_at.isoformat()}
            for s in catalog.snapshots(name)
        ]
        for name in catalog.table_names()
    }
    path.write_text(json.dumps(timeline, indent=2), encoding="utf-8")


def summary_dict(summary: WorldSummary) -> dict:
    return {**asdict(summary), "late_rate": round(summary.late_rate, 4)}
