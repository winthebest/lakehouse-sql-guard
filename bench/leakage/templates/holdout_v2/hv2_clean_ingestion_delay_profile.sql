-- leak: clean
-- param stat: AVG | MAX
-- Uses ingested_at as a value, but only of purchases already loaded before t: how late the
-- user's known purchases typically arrived.
SELECT l.user_id, l.label_ts, l.churned,
       {stat}(date_diff('hour', e.event_ts, e.ingested_at)) AS load_delay_hours,
       COUNT(e.event_id) FILTER (WHERE e.ingested_at > e.event_ts + INTERVAL 2 DAY) AS n_late_loads
FROM labels l
LEFT JOIN events e
  ON e.user_id = l.user_id
 AND e.ingested_at < l.label_ts
GROUP BY l.user_id, l.label_ts, l.churned
