-- leak: L3
-- param where: e.amount > 50 | e.event_id IS NOT NULL
SELECT l.user_id, l.label_ts, l.churned,
       SUM(e.amount) FILTER (WHERE e.ingested_at < l.label_ts) AS amount_before
FROM labels l
LEFT JOIN events e ON e.user_id = l.user_id
WHERE {where}
GROUP BY l.user_id, l.label_ts, l.churned
