-- leak: L4
-- param lag: 2 | 4 | 6
-- param agg: SUM | COUNT
SELECT l.user_id, l.label_ts, l.churned, {agg}(e.amount) AS amount_lagged
FROM labels l
LEFT JOIN events e ON e.user_id = l.user_id
  AND e.event_ts < l.label_ts - INTERVAL {lag} DAY
  AND e.event_ts >= l.label_ts - INTERVAL 90 DAY
GROUP BY l.user_id, l.label_ts, l.churned
