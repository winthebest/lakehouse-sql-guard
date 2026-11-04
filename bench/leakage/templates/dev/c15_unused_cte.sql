-- leak: clean
-- param agg: SUM | AVG | MAX | COUNT
-- param join: JOIN | LEFT JOIN
WITH unused_totals AS (SELECT user_id, {agg}(amount) AS lifetime FROM events GROUP BY user_id),
     unused_again AS (SELECT * FROM unused_totals)
SELECT l.user_id, l.label_ts, l.churned, u.country
FROM labels l
{join} entities u ON u.user_id = l.user_id
