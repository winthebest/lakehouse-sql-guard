-- leak: L7
-- param clock: current_timestamp | now()
-- param agg: SUM | COUNT
SELECT l.user_id, l.label_ts, l.churned, {agg}(e.amount) AS amount_until_today
FROM labels l
LEFT JOIN events e ON e.user_id = l.user_id
  AND e.ingested_at < l.label_ts
  AND e.event_ts <= {clock}
GROUP BY l.user_id, l.label_ts, l.churned
