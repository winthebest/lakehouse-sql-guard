-- leak: clean
-- param fallback: 0 | -1
-- COALESCE chain over two point-in-time sources: the newest stats snapshot stamped before t,
-- else the sum of purchases loaded before t, else a constant.
WITH stats_known AS (
    SELECT l.user_id, l.label_ts, max_by(s.total_spend, s.updated_at) AS spend
    FROM labels l
    JOIN user_stats s ON s.user_id = l.user_id AND s.updated_at < l.label_ts
    GROUP BY l.user_id, l.label_ts
),
events_known AS (
    SELECT l.user_id, l.label_ts, SUM(e.amount) AS spend
    FROM labels l
    JOIN events e ON e.user_id = l.user_id AND e.ingested_at < l.label_ts
    GROUP BY l.user_id, l.label_ts
)
SELECT l.user_id, l.label_ts, l.churned,
       COALESCE(sk.spend, ek.spend, {fallback}) AS lifetime_spend_best_known,
       CASE WHEN sk.spend IS NOT NULL THEN 'stats'
            WHEN ek.spend IS NOT NULL THEN 'events'
            ELSE 'none' END AS spend_source
FROM labels l
LEFT JOIN stats_known sk  ON sk.user_id = l.user_id AND sk.label_ts = l.label_ts
LEFT JOIN events_known ek ON ek.user_id = l.user_id AND ek.label_ts = l.label_ts
