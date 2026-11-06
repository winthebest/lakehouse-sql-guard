-- leak: clean
-- param gap: 8 | 10 | 14
-- param w: 14 | 60
-- No ingestion bound is needed: an event is loaded less than 8 days after its event_ts,
-- so every event with event_ts < t - gap (gap >= 8 days) was already loaded at t.
SELECT l.user_id, l.label_ts, l.churned,
       COALESCE(SUM(e.amount), 0) AS spend_matured,
       COUNT(e.event_id)          AS n_matured,
       MAX(e.event_ts)            AS last_matured_purchase_ts
FROM labels l
LEFT JOIN events e
  ON e.user_id = l.user_id
 AND e.event_ts <  l.label_ts - INTERVAL {gap} DAY
 AND e.event_ts >= l.label_ts - INTERVAL {gap} DAY - INTERVAL {w} DAY
GROUP BY ALL
