-- leak: L1
WITH spine AS (
    SELECT user_id, label_ts, churned FROM marts.fct_training_spine
),
spine_bounds AS (
    SELECT user_id, MIN(label_ts) AS first_prediction_ts, MAX(label_ts) AS last_prediction_ts
    FROM spine
    GROUP BY user_id
),
int_orders AS (
    SELECT sp.user_id, sp.label_ts, COUNT(e.event_id) AS n_orders_7d
    FROM spine sp
    LEFT JOIN staging.stg_events e
           ON e.user_id = sp.user_id
          AND e.ingested_at < sp.label_ts
          AND e.event_ts >= sp.label_ts - INTERVAL 7 DAY
    GROUP BY sp.user_id, sp.label_ts
)
SELECT sp.user_id, sp.label_ts, sp.churned,
       o.n_orders_7d,
       date_diff('hour', b.first_prediction_ts, sp.label_ts) AS hours_since_first_prediction,
       date_diff('hour', sp.label_ts, b.last_prediction_ts)  AS hours_to_last_prediction
FROM spine sp
JOIN spine_bounds b ON b.user_id = sp.user_id
LEFT JOIN int_orders o ON o.user_id = sp.user_id AND o.label_ts = sp.label_ts
