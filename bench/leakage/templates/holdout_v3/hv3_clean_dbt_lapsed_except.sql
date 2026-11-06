-- leak: clean
-- param r: 21 | 30
-- Lapsed buyer: a known purchase in the earlier window but none in the recent window.
WITH known AS (
    SELECT l.user_id, l.label_ts, e.event_ts
    FROM labels l
    JOIN staging.stg_events e
      ON e.user_id = l.user_id
     AND e.ingested_at < l.label_ts
     AND e.event_ts >= l.label_ts - INTERVAL 60 DAY
),
earlier AS (
    SELECT user_id, label_ts FROM known WHERE event_ts <  label_ts - INTERVAL {r} DAY
),
recent AS (
    SELECT user_id, label_ts FROM known WHERE event_ts >= label_ts - INTERVAL {r} DAY
),
lapsed AS (
    SELECT user_id, label_ts FROM earlier
    EXCEPT
    SELECT user_id, label_ts FROM recent
)
SELECT l.user_id, l.label_ts, l.churned, lp.user_id IS NOT NULL AS is_lapsed
FROM labels l
LEFT JOIN lapsed lp ON lp.user_id = l.user_id AND lp.label_ts = l.label_ts
