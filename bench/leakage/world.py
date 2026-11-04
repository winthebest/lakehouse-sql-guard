"""Synthetic churn world written to Iceberg as a real, multi-snapshot history.

Timeline semantics (all timestamps naive UTC):
- `events` arrive in nightly batches: an event on day d is normally ingested at midnight d+1;
  a fraction arrives late (extra 1-7 days). One Iceberg commit per ingestion night.
- `user_stats` is recomputed every midnight from events ingested so far; one commit per night.
- `entities` is static and committed before the world starts.
- `labels` (the prediction spine) and `user_outcomes` (facts observed after churn) are
  committed at the end, so they were never visible at prediction time.
- `plan_history` is a slowly changing dimension (SCD2): one row per version of a user's plan,
  `valid_from` (when the plan took effect) and `valid_to` (when the next one did; NULL while open),
  and `ingested_at` (the night the row was first loaded). A nightly job loads new versions and, in
  the same commit, closes the previous version of the same user, so `valid_to` becomes visible
  only when the superseding version does. It is generated after every other table from its own
  random stream: the other tables do not depend on it.

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
from pyiceberg.expressions import In

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
    # keep last: tables are created in this order and nothing above depends on it
    "plan_history": pa.schema(
        [
            ("user_id", pa.int64()),
            ("plan", pa.string()),
            ("monthly_fee", pa.float64()),
            ("valid_from", pa.timestamp("us")),
            ("valid_to", pa.timestamp("us")),
            ("ingested_at", pa.timestamp("us")),
        ]
    ),
}

PLAN_FEES = {"free": 0.0, "basic": 9.0, "pro": 29.0}  # in ladder order, cheapest first


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
    fade_days: int = 10
    late_max_days: int = 7  # a late event arrives 1..late_max_days nights after the usual night
    plan_change_rate: float = 0.02  # chance per day that a user's plan changes
    plan_churn_change_rate: float = 0.2  # the same, in the `plan_churn_days` before churning
    plan_churn_days: int = 8  # a churner tends to downgrade in this window before leaving
    plan_initial_age_days: int = 6  # plans loaded at the start began up to this many days earlier


def declared_spec_fields(config: WorldConfig | None = None) -> dict[str, object]:
    """The spec values this world's parameters guarantee (bench/leakage/spec.yaml repeats them).

    An event is ingested at midnight 1 night after its day, or up to `late_max_days` nights
    later, so `ingested_at - event_ts` stays below `late_max_days + 1` days. A label covers the
    `horizon_days` after its prediction time, so it is final once the horizon has passed.
    """
    cfg = config or WorldConfig()
    return {
        "max_ingest_delay": {"events": f"{cfg.late_max_days + 1} days"},
        "label_known_after": f"{cfg.horizon_days} days",
    }


def declared_plan_history_fields(config: WorldConfig | None = None) -> dict[str, object]:
    """The `plan_history` spec values this world's parameters guarantee.

    A version is loaded the night after it took effect, or up to `late_max_days` nights later
    (never before the version it replaces), and the versions in place at the start began at most
    `plan_initial_age_days` earlier, so `ingested_at - valid_from` stays below the larger of the
    two delays.
    """
    cfg = config or WorldConfig()
    days = max(cfg.late_max_days + 1, cfg.plan_initial_age_days + 1)
    return {"max_ingest_delay": {"plan_history": f"{days} days"}}


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


@dataclass(frozen=True)
class PlanVersion:
    """One row of `plan_history` with the times the generator knows for it."""

    user_id: int
    plan: str
    monthly_fee: float
    valid_from: datetime  # when the plan took effect
    ingested_at: datetime  # the night the row was first loaded: from then on it is knowable
    valid_to: datetime | None  # when the next version took effect; None while this one is open
    closed_at: datetime | None  # the night the next version was loaded: `valid_to` is knowable

    def as_row(self, now: datetime) -> dict:
        """The row as the table holds it after the load of night `now`."""
        closed = self.closed_at is not None and self.closed_at <= now
        return {
            "user_id": self.user_id,
            "plan": self.plan,
            "monthly_fee": self.monthly_fee,
            "valid_from": self.valid_from,
            "valid_to": self.valid_to if closed else None,
            "ingested_at": self.ingested_at,
        }


def generate_plan_versions(cfg: WorldConfig, churn_day: dict[int, int | None]) -> list[PlanVersion]:
    """Every plan version that is loaded before the world ends, from a stream of its own.

    A user starts on a plan that began up to `plan_initial_age_days` before the world, changes
    plan on random days (more often, and downwards, shortly before churning), and each change is
    loaded the night after it took effect or, for `late_rate` of them, up to `late_max_days`
    nights later. A version is never loaded before the one it replaces. Changes still unloaded
    when the world ends are not part of the table.
    """
    rng = random.Random(f"plan_history/{cfg.seed}")
    ladder = list(PLAN_FEES)
    versions: list[PlanVersion] = []
    for user in range(1, cfg.n_users + 1):
        plan = rng.choices(ladder, weights=[0.5, 0.35, 0.15])[0]
        taken = [
            (plan, START - timedelta(seconds=rng.randint(1, cfg.plan_initial_age_days * 86_400)), 0)
        ]
        last_ingest = 0
        churn = churn_day.get(user)
        for day in range(cfg.n_days):
            if churn is not None and day >= churn:
                break
            leaving = churn is not None and churn - day <= cfg.plan_churn_days
            if rng.random() >= (cfg.plan_churn_change_rate if leaving else cfg.plan_change_rate):
                continue
            rung = ladder.index(plan)
            options = [r for r in (rung - 1, rung + 1) if 0 <= r < len(ladder)]
            if leaving:
                options = [r for r in options if r < rung]
            if not options:
                continue
            plan = ladder[rng.choice(options)]
            started = _midnight(day) + timedelta(seconds=rng.randint(0, 86_399))
            late = rng.random() < cfg.late_rate
            ingest_day = max(
                day + 1 + (rng.randint(1, cfg.late_max_days) if late else 0), last_ingest
            )
            if ingest_day > cfg.n_days:
                break  # in flight when the world ends, and so is everything after it
            last_ingest = ingest_day
            taken.append((plan, started, ingest_day))
        for k, (name, started, ingest_day) in enumerate(taken):
            after = taken[k + 1] if k + 1 < len(taken) else None
            versions.append(
                PlanVersion(
                    user_id=user,
                    plan=name,
                    monthly_fee=PLAN_FEES[name],
                    valid_from=started,
                    ingested_at=_midnight(ingest_day),
                    valid_to=after[1] if after else None,
                    closed_at=_midnight(after[2]) if after else None,
                )
            )
    return versions


def _commit_plan_history(catalog: LakehouseCatalog, versions: list[PlanVersion], n_days: int):
    """Load the versions night by night, closing a user's open version in the load of the next.

    Closing a version changes a row that is already stored, so the nightly commit replaces the rows
    of the users it touches (an Iceberg overwrite: a delete snapshot and an append snapshot that
    carry the same logical time, and become visible together).
    """
    by_night: dict[datetime, list[PlanVersion]] = defaultdict(list)
    for v in versions:
        by_night[v.ingested_at].append(v)
    history: dict[int, list[PlanVersion]] = defaultdict(list)
    for night in range(n_days + 1):
        now = _midnight(night)
        loaded = by_night.get(now, [])
        if not loaded:
            continue
        for v in loaded:
            history[v.user_id].append(v)
        touched = sorted({v.user_id for v in loaded})
        rows = [v.as_row(now) for u in touched for v in history[u]]
        data = pa.Table.from_pylist(rows, schema=SCHEMAS["plan_history"])
        table = catalog.table("plan_history")
        properties = {LOGICAL_TS_PROPERTY: now.isoformat()}
        if table.current_snapshot() is None:
            table.append(data, snapshot_properties=properties)
        else:
            table.overwrite(
                data, overwrite_filter=In("user_id", touched), snapshot_properties=properties
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
        if rng.random() < cfg.churn_rate * 2 * (2.0 - rates[u]) / 1.8  # low activity churns more
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
            rate = rates[u]
            if churn_day[u] is not None and churn_day[u] - day <= cfg.fade_days:
                # Churners fade out before leaving, so past behavior carries real signal.
                rate *= (churn_day[u] - day) / (cfg.fade_days + 1)
            for _ in range(_poisson(rng, rate)):
                late = rng.random() < cfg.late_rate
                ingest_day = day + 1 + (rng.randint(1, cfg.late_max_days) if late else 0)
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

    _commit_plan_history(catalog, generate_plan_versions(cfg, churn_day), cfg.n_days)

    return WorldSummary(
        n_events=sum(len(rows) for rows in by_ingest_day.values()),
        n_late_events=n_late,
        n_labels=len(labels),
        churn_rate=sum(r["churned"] for r in labels) / len(labels),
        snapshots={name: len(catalog.snapshots(name)) for name in SCHEMAS},
    )


# --- ground truth for `plan_history`, from the times the generator wrote ---------------------
# A version is knowable at prediction time `t` once its `ingested_at` is before `t`; the question
# "which plan was in effect" has a second, event-time answer that can differ from it. Labels for
# queries over this table follow from the two, never from replaying snapshots.


def plan_versions(catalog: LakehouseCatalog) -> list[PlanVersion]:
    """The generator's versions, read back from the finished table (final `valid_to` included)."""
    rows = sorted(
        catalog.read("plan_history").to_pylist(), key=lambda r: (r["user_id"], r["valid_from"])
    )
    versions = []
    for k, row in enumerate(rows):
        following = (
            rows[k + 1] if k + 1 < len(rows) and rows[k + 1]["user_id"] == row["user_id"] else None
        )
        versions.append(
            PlanVersion(
                user_id=row["user_id"],
                plan=row["plan"],
                monthly_fee=row["monthly_fee"],
                valid_from=row["valid_from"],
                ingested_at=row["ingested_at"],
                valid_to=row["valid_to"],
                closed_at=following["ingested_at"] if following else None,
            )
        )
    return versions


def known_plans(versions: list[PlanVersion], t: datetime) -> dict[int, PlanVersion]:
    """Per user, the newest version loaded strictly before `t`: what a model could read at `t`."""
    known: dict[int, PlanVersion] = {}
    for v in sorted(versions, key=lambda v: v.valid_from):
        if v.ingested_at < t:
            known[v.user_id] = v
    return known


def effective_plans(versions: list[PlanVersion], t: datetime) -> dict[int, PlanVersion]:
    """Per user, the version in effect just before `t` by event time, loaded yet or not."""
    effective: dict[int, PlanVersion] = {}
    for v in sorted(versions, key=lambda v: v.valid_from):
        if v.valid_from < t:
            effective[v.user_id] = v
    return effective


def late_plan_users(versions: list[PlanVersion], t: datetime) -> set[int]:
    """Users whose plan in effect at `t` was not yet loaded: an event-time-only lookup leaks."""
    known = known_plans(versions, t)
    return {user for user, v in effective_plans(versions, t).items() if known.get(user) != v}


def open_versions_at(versions: list[PlanVersion], t: datetime) -> list[PlanVersion]:
    """Versions in effect at `t` that the finished table closes later.

    Their final `valid_to` is a change nobody knew of at `t`: a feature built from it leaks, even
    though it is only a timestamp column of a row loaded before `t`.
    """
    return [v for v in versions if v.valid_from < t and v.valid_to is not None and v.valid_to >= t]


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
