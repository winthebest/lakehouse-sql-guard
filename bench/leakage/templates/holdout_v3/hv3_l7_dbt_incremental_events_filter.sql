-- leak: L7
-- param d: 90 | 365
WITH stg_events AS (
    -- incremental-style lookback relative to the run time
    SELECT event_id, user_id, event_ts, ingested_at, amount
    FROM analytics.staging.stg_events
    WHERE event_ts >= now() - INTERVAL {d} DAY
),
spine AS (
    SELECT user_id, label_ts, churned FROM analytics.marts.fct_training_spine
),
int_features AS (
    SELECT sp.user_id, sp.label_ts, sp.churned,
           COUNT(e.event_id)          AS n_orders_30d,
           COALESCE(SUM(e.amount), 0) AS spend_30d
    FROM spine sp
    LEFT JOIN stg_events e
           ON e.user_id = sp.user_id
          AND e.ingested_at < sp.label_ts
          AND e.event_ts >= sp.label_ts - INTERVAL 30 DAY
    GROUP BY ALL
)
SELECT * FROM int_features
