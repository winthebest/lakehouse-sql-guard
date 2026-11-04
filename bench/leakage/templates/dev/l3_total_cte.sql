-- leak: L3
-- param agg: SUM | AVG | MAX | COUNT
-- param join: JOIN | LEFT JOIN
WITH tot AS (SELECT user_id, {agg}(amount) AS lifetime FROM events GROUP BY user_id)
SELECT l.user_id, l.label_ts, l.churned, t.lifetime
FROM labels l
{join} tot t ON t.user_id = l.user_id
