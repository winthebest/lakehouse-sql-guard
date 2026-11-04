-- leak: L5
-- param cond: epoch(e.ingested_at) <= epoch(l.label_ts) | date_diff('second', e.ingested_at, l.label_ts) >= 0 | l.label_ts - e.ingested_at >= INTERVAL 0 DAY
-- param agg: SUM | COUNT
SELECT l.user_id, l.label_ts, l.churned, {agg}(e.amount) AS amount_14d
FROM labels l
LEFT JOIN events e ON e.user_id = l.user_id
  AND {cond}
  AND e.event_ts >= l.label_ts - INTERVAL 14 DAY
GROUP BY l.user_id, l.label_ts, l.churned
