-- leak: L3
-- param agg: SUM | AVG | MAX
SELECT l.user_id, l.label_ts, l.churned,
       {agg}(e.amount) FILTER (WHERE e.ingested_at < l.label_ts) AS amount_before,
       COUNT(e.event_id) AS n_events
FROM labels l
LEFT JOIN events e ON e.user_id = l.user_id
GROUP BY l.user_id, l.label_ts, l.churned
