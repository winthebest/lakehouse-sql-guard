-- leak: clean
-- param agg: SUM | AVG | MAX | COUNT
-- param window: 7 | 14 | 30 | 90
-- param join: JOIN | LEFT JOIN
SELECT l.user_id, l.label_ts, l.churned, {agg}(e.amount) AS amount_{window}d
FROM labels l
{join} events e ON e.user_id = l.user_id
  AND e.ingested_at < l.label_ts
  AND e.event_ts >= l.label_ts - INTERVAL {window} DAY
GROUP BY l.user_id, l.label_ts, l.churned
