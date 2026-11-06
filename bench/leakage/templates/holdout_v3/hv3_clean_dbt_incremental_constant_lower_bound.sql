-- leak: clean
-- param since: 2000-01-01 | 2015-01-01
WITH stg_events AS (
    -- incremental-style lookback, fixed at build time rather than relative to the run
    SELECT event_id, user_id, event_ts, ingested_at AS loaded_at, amount
    FROM staging.stg_events
    WHERE event_ts >= TIMESTAMP '{since}'
),
spine AS (
    SELECT user_id, label_ts, churned FROM marts.fct_training_spine
),
int_orders AS (
    SELECT sp.user_id, sp.label_ts,
           COUNT(e.event_id) AS n_orders_known,
           AVG(e.amount)     AS avg_order_known
    FROM spine sp
    LEFT JOIN stg_events e
           ON e.user_id = sp.user_id
          AND e.loaded_at < sp.label_ts
    GROUP BY sp.user_id, sp.label_ts
)
SELECT sp.user_id, sp.label_ts, sp.churned, o.n_orders_known, o.avg_order_known
FROM spine sp
LEFT JOIN int_orders o ON o.user_id = sp.user_id AND o.label_ts = sp.label_ts
