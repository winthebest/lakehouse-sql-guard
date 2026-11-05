-- leak: L7
-- param days: 30 | 90 | 365
-- Purchases are point-in-time (ingested_at < t), but the window's lower edge is anchored on
-- current_date: which purchases fall in it depends on the day of the run.
SELECT l.user_id, l.label_ts, l.churned,
       COUNT(e.event_id) AS n_known_purchases,
       COALESCE(SUM(e.amount) FILTER (WHERE e.event_ts >= current_date - INTERVAL {days} DAY), 0)
           AS spend_last_{days}d_of_run
FROM labels l
LEFT JOIN events e
  ON e.user_id = l.user_id
 AND e.ingested_at < l.label_ts
GROUP BY l.user_id, l.label_ts, l.churned
