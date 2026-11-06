-- leak: clean
-- param w: 7 | 28
-- Deduplication keeps the EARLIEST load of an event, so a copy loaded after t can never
-- replace one that was already known.
WITH stg_events AS (
    SELECT event_id, user_id, event_ts, ingested_at, amount
    FROM staging.stg_events
    QUALIFY row_number() OVER (PARTITION BY event_id ORDER BY ingested_at, amount) = 1
),
spine AS (
    SELECT md5(CAST(user_id AS VARCHAR) || '|' || CAST(label_ts AS VARCHAR)) AS spine_key,
           user_id, label_ts, churned
    FROM marts.fct_training_spine
),
int_features AS (
    SELECT sp.spine_key,
           COUNT(e.event_id) AS n_orders_window,
           SUM(e.amount)     AS spend_window
    FROM spine sp
    LEFT JOIN stg_events e
           ON e.user_id = sp.user_id
          AND e.ingested_at < sp.label_ts
          AND e.event_ts >= sp.label_ts - INTERVAL {w} DAY
    GROUP BY sp.spine_key
)
SELECT sp.spine_key, sp.user_id, sp.label_ts, sp.churned, f.n_orders_window, f.spend_window
FROM spine sp
LEFT JOIN int_features f ON f.spine_key = sp.spine_key
