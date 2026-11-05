-- leak: L4
-- param days: 1 | 3 | 7
-- param pred: EXISTS | NOT EXISTS
-- The EXISTS is bounded by event time only; a purchase in the last few days was typically
-- loaded at the next midnight or later, possibly after t.
SELECT l.user_id, l.label_ts, l.churned,
       CASE WHEN {pred} (
                SELECT 1
                FROM events e
                WHERE e.user_id = l.user_id
                  AND e.event_ts <  l.label_ts
                  AND e.event_ts >= l.label_ts - INTERVAL {days} DAY)
            THEN 1 ELSE 0 END AS recent_purchase_flag
FROM labels l
