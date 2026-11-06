-- leak: L4
-- param d: 7 | 21
WITH spine AS (
    SELECT user_id, label_ts, churned FROM marts.fct_training_spine
)
SELECT sp.user_id, sp.label_ts, sp.churned, FALSE AS is_dormant
FROM spine sp
SEMI JOIN staging.stg_events e
       ON e.user_id = sp.user_id
      AND e.event_ts <  sp.label_ts
      AND e.event_ts >= sp.label_ts - INTERVAL {d} DAY
UNION ALL
SELECT sp.user_id, sp.label_ts, sp.churned, TRUE AS is_dormant
FROM spine sp
ANTI JOIN staging.stg_events e
       ON e.user_id = sp.user_id
      AND e.event_ts <  sp.label_ts
      AND e.event_ts >= sp.label_ts - INTERVAL {d} DAY
