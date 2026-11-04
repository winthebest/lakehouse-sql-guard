-- leak: L3
-- param count: COUNT(1) | SUM(1) | COUNT(l.user_id) | count_star() | product(2)
-- param agg: SUM | MAX
SELECT l.user_id, l.label_ts, l.churned,
       {agg}(e.amount) FILTER (WHERE e.ingested_at < l.label_ts) AS amount_before,
       {count} AS n_joined
FROM labels l
LEFT JOIN events e ON e.user_id = l.user_id
GROUP BY l.user_id, l.label_ts, l.churned
