-- leak: L3
-- param agg: SUM | COUNT | AVG
-- param bound: MAX(e.ingested_at) < l.label_ts | MAX(e.ingested_at) <= l.label_ts - INTERVAL 1 SECOND
SELECT l.user_id, l.label_ts, l.churned, {agg}(e.amount) AS amount_14d
FROM labels l
LEFT JOIN events e ON e.user_id = l.user_id AND e.event_ts >= l.label_ts - INTERVAL 14 DAY
GROUP BY l.user_id, l.label_ts, l.churned
HAVING {bound}
