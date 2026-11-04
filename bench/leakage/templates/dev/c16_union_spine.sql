-- leak: clean
-- param set: UNION ALL | UNION
-- param agg: SUM | COUNT | MAX
SELECT sp.user_id, sp.label_ts, sp.churned, {agg}(e.amount) AS amount_14d
FROM (
  SELECT user_id, label_ts, churned FROM labels WHERE user_id % 2 = 0
  {set}
  SELECT user_id, label_ts, churned FROM labels WHERE user_id % 2 = 1
) sp
LEFT JOIN events e ON e.user_id = sp.user_id
  AND e.ingested_at < sp.label_ts
  AND e.event_ts >= sp.label_ts - INTERVAL 14 DAY
GROUP BY sp.user_id, sp.label_ts, sp.churned
