-- leak: L2
-- param col: s.total_spend | s.n_orders | s.total_spend / NULLIF(s.n_orders, 0)
-- param join: JOIN | LEFT JOIN
SELECT l.user_id, l.label_ts, l.churned, {col} AS feat
FROM labels l
{join} user_stats s ON s.user_id = l.user_id
QUALIFY row_number() OVER (PARTITION BY l.user_id, l.label_ts ORDER BY s.updated_at DESC) = 1
