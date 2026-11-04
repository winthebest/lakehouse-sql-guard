-- leak: clean
-- param cut: (SELECT MAX(label_ts) FROM labels) | (SELECT MAX(label_ts) FROM labels) - INTERVAL 3 DAY | (SELECT MIN(label_ts) FROM labels) + INTERVAL 10 DAY
-- param agg: SUM | COUNT
SELECT l.user_id, l.label_ts, l.churned, {agg}(e.amount) AS amount_14d
FROM labels l
LEFT JOIN events e ON e.user_id = l.user_id
  AND e.ingested_at < l.label_ts
  AND e.event_ts >= l.label_ts - INTERVAL 14 DAY
WHERE l.label_ts < {cut}
GROUP BY l.user_id, l.label_ts, l.churned
