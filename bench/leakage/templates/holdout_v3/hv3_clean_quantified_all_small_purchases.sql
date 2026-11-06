-- leak: clean
-- param cap: 20 | 40
SELECT l.user_id, l.label_ts, l.churned,
       {cap} > ALL (SELECT e.amount FROM events e
                     WHERE e.user_id = l.user_id
                       AND e.ingested_at < l.label_ts
                       AND e.event_ts >= l.label_ts - INTERVAL 30 DAY) AS only_small_purchases_30d
FROM labels l
