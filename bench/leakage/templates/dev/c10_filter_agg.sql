-- leak: clean
-- param agg: SUM | AVG | MAX | COUNT
-- param window: 7 | 30
SELECT l.user_id, l.label_ts, l.churned,
       {agg}(e.amount) FILTER (WHERE e.ingested_at < l.label_ts
                                 AND e.event_ts >= l.label_ts - INTERVAL {window} DAY) AS amount_{window}d,
       COUNT(e.event_id) FILTER (WHERE e.ingested_at < l.label_ts) AS n_events
FROM labels l
LEFT JOIN events e ON e.user_id = l.user_id
GROUP BY l.user_id, l.label_ts, l.churned
