-- leak: clean
SELECT l.user_id, l.label_ts, l.churned,
       COALESCE(
           (SELECT s.total_spend FROM user_stats s
             WHERE s.user_id = l.user_id AND s.updated_at < l.label_ts
             ORDER BY s.updated_at DESC LIMIT 1),
           (SELECT SUM(e.amount) FROM events e
             WHERE e.user_id = l.user_id AND e.ingested_at < l.label_ts),
           0) AS lifetime_spend_best_known
FROM labels l
