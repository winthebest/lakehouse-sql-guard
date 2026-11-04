-- leak: clean
-- param window: 7 | 14 | 30 | 90
SELECT l.user_id, l.label_ts, l.churned,
       date_diff('hour', MAX(e.event_ts), l.label_ts) AS hours_since_last
FROM labels l
LEFT JOIN events e ON e.user_id = l.user_id AND e.ingested_at < l.label_ts
  AND e.event_ts >= l.label_ts - INTERVAL {window} DAY
GROUP BY l.user_id, l.label_ts, l.churned
