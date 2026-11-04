-- leak: L1
-- param asof: ASOF JOIN | ASOF LEFT JOIN
SELECT l.user_id, l.label_ts, l.churned, s.total_spend, s.n_orders
FROM labels l
{asof} user_stats s ON l.user_id = s.user_id AND l.label_ts < s.updated_at
