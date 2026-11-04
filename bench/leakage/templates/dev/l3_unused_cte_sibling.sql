-- leak: L3
-- param agg: SUM | AVG | COUNT
WITH unused AS (SELECT user_id, MAX(amount) AS top FROM events GROUP BY user_id),
     tot AS (SELECT user_id, {agg}(amount) AS lifetime FROM events GROUP BY user_id)
SELECT l.user_id, l.label_ts, l.churned, t.lifetime
FROM labels l
LEFT JOIN tot t ON t.user_id = l.user_id
