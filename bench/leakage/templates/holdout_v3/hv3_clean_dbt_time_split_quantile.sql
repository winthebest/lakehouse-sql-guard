-- leak: clean
-- param q: 0.1 | 0.25
-- Drop the earliest prediction times (warm-up) with a quantile cutoff read from the spine itself.
WITH spine AS (
    SELECT user_id, label_ts, churned
    FROM analytics.marts.fct_training_spine
    WHERE label_ts >= (SELECT quantile_cont(label_ts, {q}) FROM analytics.marts.fct_training_spine)
),
stg_events AS (
    SELECT user_id, event_ts, ingested_at AS loaded_at, amount
    FROM analytics.staging.stg_events
),
int_activity AS (
    SELECT sp.user_id, sp.label_ts, sp.churned,
           COUNT(e.amount)              AS n_orders_14d,
           COUNT(DISTINCT e.event_ts::DATE) AS active_days_14d
    FROM spine sp
    LEFT JOIN stg_events e
           ON e.user_id = sp.user_id
          AND e.loaded_at < sp.label_ts
          AND e.event_ts >= sp.label_ts - INTERVAL 14 DAY
    GROUP BY ALL
)
SELECT * FROM int_activity
