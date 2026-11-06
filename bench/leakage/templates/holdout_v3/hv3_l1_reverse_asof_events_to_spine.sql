-- leak: L1
WITH spine AS (
    SELECT user_id, label_ts, churned FROM marts.fct_training_spine
),
stg_events AS (
    SELECT event_id, user_id, event_ts, amount FROM staging.stg_events
),
int_event_to_prediction AS (
    -- attribute each purchase to the prediction time it follows
    SELECT e.event_id, e.amount, sp.user_id, sp.label_ts
    FROM stg_events e
    ASOF JOIN spine sp
      ON sp.user_id = e.user_id
     AND e.event_ts >= sp.label_ts
)
SELECT sp.user_id, sp.label_ts, sp.churned,
       COUNT(i.event_id) AS n_events_attributed,
       SUM(i.amount)     AS spend_attributed
FROM spine sp
LEFT JOIN int_event_to_prediction i
       ON i.user_id = sp.user_id
      AND i.label_ts = sp.label_ts
GROUP BY ALL
