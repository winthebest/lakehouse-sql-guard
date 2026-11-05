-- leak: L2
-- param col: total_spend | n_orders
-- param join: LEFT JOIN | JOIN
-- Joins the globally latest recompute (MAX(updated_at) over the table), with no bound on t.
SELECT l.user_id, l.label_ts, l.churned,
       s.{col} AS snapshot_{col}
FROM labels l
{join} user_stats s
  ON s.user_id = l.user_id
 AND s.updated_at = (SELECT MAX(updated_at) FROM user_stats)
