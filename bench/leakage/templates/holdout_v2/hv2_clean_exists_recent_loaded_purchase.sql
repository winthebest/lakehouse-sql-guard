-- leak: clean
-- param days: 3 | 7 | 14
-- param pred: EXISTS | NOT EXISTS
-- The correlated EXISTS only sees purchases already loaded (ingested_at < t).
SELECT l.user_id, l.label_ts, l.churned,
       CASE WHEN {pred} (
                SELECT 1
                FROM events e
                WHERE e.user_id = l.user_id
                  AND e.ingested_at < l.label_ts
                  AND e.event_ts >= l.label_ts - INTERVAL {days} DAY)
            THEN 1 ELSE 0 END AS recent_purchase_flag
FROM labels l
