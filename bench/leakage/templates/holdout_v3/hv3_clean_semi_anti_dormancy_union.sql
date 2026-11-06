-- leak: clean
-- param d: 7 | 21
WITH spine AS (
    SELECT user_id, label_ts, churned FROM labels
)
SELECT sp.user_id, sp.label_ts, sp.churned, FALSE AS is_dormant
FROM spine sp
SEMI JOIN events e
       ON e.user_id = sp.user_id
      AND e.ingested_at < sp.label_ts
      AND e.event_ts >= sp.label_ts - INTERVAL {d} DAY
UNION ALL
SELECT sp.user_id, sp.label_ts, sp.churned, TRUE AS is_dormant
FROM spine sp
ANTI JOIN events e
       ON e.user_id = sp.user_id
      AND e.ingested_at < sp.label_ts
      AND e.event_ts >= sp.label_ts - INTERVAL {d} DAY
