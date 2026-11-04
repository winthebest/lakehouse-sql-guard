-- leak: L1
-- param agg: SUM | AVG | MAX | COUNT
-- param window: 7 | 14 | 30 | 90
SELECT l.user_id, l.label_ts, l.churned, {agg}(e.amount) AS amount_{window}d
FROM labels l
LEFT JOIN events e ON e.user_id = l.user_id
  AND e.event_ts >= l.label_ts
  AND e.event_ts < l.label_ts + INTERVAL {window} DAY
GROUP BY l.user_id, l.label_ts, l.churned
