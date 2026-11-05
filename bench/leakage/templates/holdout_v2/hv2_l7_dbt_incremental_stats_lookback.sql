-- leak: L7
-- param lookback: 30 | 365 | 3650
-- An incremental-style staging filter anchored on now() trims the stats table before the
-- correct point-in-time pick, so the feature depends on the run date.
WITH stg_user_stats AS (
    SELECT user_id, updated_at, total_spend, n_orders
    FROM analytics.staging.stg_user_stats
    WHERE updated_at >= CAST(now() AS TIMESTAMP) - INTERVAL {lookback} DAY
),
spine AS (
    SELECT user_id, label_ts, churned
    FROM analytics.marts.fct_training_spine
),
int_stats_pit AS (
    SELECT sp.user_id, sp.label_ts, s.total_spend, s.n_orders
    FROM spine sp
    JOIN stg_user_stats s ON s.user_id = sp.user_id AND s.updated_at < sp.label_ts
    QUALIFY row_number() OVER (PARTITION BY sp.user_id, sp.label_ts ORDER BY s.updated_at DESC) = 1
)
SELECT sp.user_id, sp.label_ts, sp.churned,
       i.total_spend, i.n_orders
FROM spine sp
LEFT JOIN int_stats_pit i
  ON i.user_id = sp.user_id AND i.label_ts = sp.label_ts
