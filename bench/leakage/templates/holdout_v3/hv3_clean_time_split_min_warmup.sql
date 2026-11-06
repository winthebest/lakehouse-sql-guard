-- leak: clean
-- param d: 2 | 4
SELECT l.user_id, l.label_ts, l.churned,
       COUNT(DISTINCT CAST(e.event_ts AS DATE)) AS active_days_14d
FROM labels l
LEFT JOIN events e
       ON e.user_id = l.user_id
      AND e.ingested_at < l.label_ts
      AND e.event_ts >= l.label_ts - INTERVAL 14 DAY
WHERE l.label_ts >= (SELECT MIN(label_ts) FROM labels) + INTERVAL {d} DAY
GROUP BY ALL
