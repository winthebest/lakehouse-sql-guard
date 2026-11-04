-- leak: L5
-- param agg: SUM | AVG | MAX | COUNT
-- param tz: 7 | 9
SELECT l.user_id, l.label_ts, l.churned, {agg}(e.amount) AS amount_local
FROM labels l
LEFT JOIN events e ON e.user_id = l.user_id
  AND e.ingested_at < l.label_ts + INTERVAL {tz} HOUR
  AND e.event_ts >= l.label_ts - INTERVAL 30 DAY
GROUP BY l.user_id, l.label_ts, l.churned
