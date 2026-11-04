-- leak: L5
-- param asof: ASOF JOIN | ASOF LEFT JOIN
-- param col: s.total_spend | s.n_orders
SELECT l.user_id, l.label_ts, l.churned, {col} AS feat
FROM labels l
{asof} user_stats s ON l.user_id = s.user_id AND l.label_ts >= s.updated_at
