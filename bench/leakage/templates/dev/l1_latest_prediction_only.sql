-- leak: L1
-- param agg: SUM | COUNT | MAX
SELECT l.user_id, l.label_ts, l.churned, {agg}(e.amount) AS amount_14d
FROM labels l
LEFT JOIN events e ON e.user_id = l.user_id
  AND e.ingested_at < l.label_ts
  AND e.event_ts >= l.label_ts - INTERVAL 14 DAY
GROUP BY l.user_id, l.label_ts, l.churned
QUALIFY ROW_NUMBER() OVER (PARTITION BY l.user_id ORDER BY l.label_ts DESC) = 1
