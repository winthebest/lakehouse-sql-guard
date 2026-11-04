-- leak: L3
-- param agg: SUM | AVG | MAX | COUNT
-- param join: JOIN | LEFT JOIN
SELECT l.user_id, l.label_ts, l.churned, {agg}(e.amount) AS lifetime
FROM labels l
{join} events e ON e.user_id = l.user_id
GROUP BY l.user_id, l.label_ts, l.churned
