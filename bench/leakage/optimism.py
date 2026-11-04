"""Optimism gap: how much a leaky feature query inflates offline AUC.

The same model (scaled logistic regression, fixed config) is trained on features from a
clean query and from a leaky one. Each is evaluated twice on a later time period:
- offline AUC: features from the query run on today's data (what the data scientist sees);
- real AUC: features from the same query run on the snapshots that were visible at each
  prediction time (what the model actually receives when it serves).
For a clean query both numbers match; for a leaky one the offline number is optimistic.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import pyarrow as pa
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from sqlguard.core.catalog import LakehouseCatalog
from sqlguard.leakage.replay import Replayer
from sqlguard.leakage.spec import LeakageSpec

CLEAN_QUERY = """
WITH ev AS (
  SELECT l.user_id, l.label_ts, COUNT(e.event_id) AS n_recent, MAX(e.event_ts) AS last_ts
  FROM labels l
  LEFT JOIN events e ON e.user_id = l.user_id AND e.ingested_at < l.label_ts
    AND e.event_ts >= l.label_ts - INTERVAL 14 DAY
  GROUP BY l.user_id, l.label_ts
)
SELECT l.user_id, l.label_ts, l.churned,
       coalesce(s.total_spend, 0) AS total_spend, coalesce(s.n_orders, 0) AS n_orders,
       ev.n_recent, coalesce(date_diff('hour', ev.last_ts, l.label_ts), 336) AS hours_since_last
FROM labels l
ASOF LEFT JOIN user_stats s ON l.user_id = s.user_id AND l.label_ts > s.updated_at
JOIN ev ON ev.user_id = l.user_id AND ev.label_ts = l.label_ts
"""

# The common mistake: "latest" stats and whole-history aggregates (L2 + L3).
LEAKY_QUERY = """
WITH ev AS (SELECT user_id, COUNT(*) AS n_recent, MAX(event_ts) AS last_ts
            FROM events GROUP BY user_id),
latest AS (SELECT user_id, total_spend, n_orders FROM user_stats
           QUALIFY row_number() OVER (PARTITION BY user_id ORDER BY updated_at DESC) = 1)
SELECT l.user_id, l.label_ts, l.churned,
       coalesce(s.total_spend, 0) AS total_spend, coalesce(s.n_orders, 0) AS n_orders,
       coalesce(ev.n_recent, 0) AS n_recent,
       coalesce(date_diff('hour', ev.last_ts, l.label_ts), 336) AS hours_since_last
FROM labels l
LEFT JOIN latest s ON s.user_id = l.user_id
LEFT JOIN ev ON ev.user_id = l.user_id
"""


@dataclass(frozen=True)
class GapResult:
    offline_auc: float
    real_auc: float

    @property
    def gap(self) -> float:
        return self.offline_auc - self.real_auc

    def as_dict(self) -> dict[str, float]:
        return {
            "offline_auc": round(self.offline_auc, 4),
            "real_auc": round(self.real_auc, 4),
            "gap": round(self.gap, 4),
        }


def _xy(table: pa.Table, spec: LeakageSpec) -> tuple[list[list[float]], list[int]]:
    features = [c for c in table.column_names if c not in spec.passthrough_columns]
    rows = table.to_pylist()
    x = [[float(r[c] or 0.0) for c in features] for r in rows]
    y = [int(r[spec.label_columns[0]]) for r in rows]
    return x, y


def _split(table: pa.Table, spec: LeakageSpec, cutoff: datetime) -> tuple[pa.Table, pa.Table]:
    stamps = table.column(spec.prediction_ts).to_pylist()
    train = table.filter(pa.array([t < cutoff for t in stamps]))
    test = table.filter(pa.array([t >= cutoff for t in stamps]))
    return train, test


def optimism_gap(
    catalog: LakehouseCatalog, spec: LeakageSpec, sql: str, cutoff: datetime
) -> GapResult:
    """Train on prediction times before `cutoff`, evaluate offline vs at-serving-time after."""
    replayer = Replayer(catalog, spec, max_points=10**9)
    current = replayer.current().sql(sql).to_arrow_table()
    train, test_offline = _split(current, spec, cutoff)
    serving = [
        replayer.pinned(ts).sql(sql).to_arrow_table() for ts in replayer.points if ts >= cutoff
    ]
    test_real = pa.concat_tables(serving)

    model = make_pipeline(StandardScaler(), LogisticRegression(max_iter=1000, random_state=0))
    model.fit(*_xy(train, spec))
    scores = {}
    for name, table in (("offline", test_offline), ("real", test_real)):
        x, y = _xy(table, spec)
        scores[name] = roc_auc_score(y, model.predict_proba(x)[:, 1])
    return GapResult(scores["offline"], scores["real"])


def run(catalog: LakehouseCatalog, spec: LeakageSpec, cutoff: datetime) -> dict[str, dict]:
    return {
        "clean": optimism_gap(catalog, spec, CLEAN_QUERY, cutoff).as_dict(),
        "leaky": optimism_gap(catalog, spec, LEAKY_QUERY, cutoff).as_dict(),
    }
