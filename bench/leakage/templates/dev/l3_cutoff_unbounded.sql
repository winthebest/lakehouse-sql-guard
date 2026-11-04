-- leak: L3
-- param cut: (SELECT MAX(label_ts) FROM labels) | (SELECT MAX(x.label_ts) FROM labels x WHERE x.user_id = l.user_id)
-- param agg: SUM | COUNT
SELECT l.user_id, l.label_ts, l.churned, {agg}(e.amount) AS lifetime
FROM labels l
LEFT JOIN events e ON e.user_id = l.user_id
WHERE l.label_ts < {cut}
GROUP BY l.user_id, l.label_ts, l.churned
