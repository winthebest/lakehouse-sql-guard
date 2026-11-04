-- leak: L5
-- param agg: SUM | COUNT | MAX
SELECT l.user_id, l.label_ts, l.churned,
       {agg}(e.amount) FILTER (WHERE e.ingested_at <= l.label_ts
                                 AND e.event_ts >= l.label_ts - INTERVAL 14 DAY) AS amount_14d
FROM labels l
LEFT JOIN events e ON e.user_id = l.user_id
GROUP BY l.user_id, l.label_ts, l.churned
