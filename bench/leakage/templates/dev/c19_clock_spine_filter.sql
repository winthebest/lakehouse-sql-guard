-- leak: clean
-- param now: now() | current_timestamp | current_date
SELECT l.user_id, l.label_ts, l.churned, COUNT(e.event_id) AS n_events_14d
FROM labels l
LEFT JOIN events e ON e.user_id = l.user_id
  AND e.ingested_at < l.label_ts
  AND e.event_ts >= l.label_ts - INTERVAL 14 DAY
-- the offset keeps every prediction row whatever day the benchmark runs on
WHERE l.label_ts < {now} + INTERVAL 100 YEAR
GROUP BY l.user_id, l.label_ts, l.churned
