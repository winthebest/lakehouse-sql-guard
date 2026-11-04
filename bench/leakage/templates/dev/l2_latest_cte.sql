-- leak: L2
-- param col: lt.total_spend | lt.n_orders | lt.total_spend / NULLIF(lt.n_orders, 0)
-- param join: JOIN | LEFT JOIN
WITH latest AS (
  SELECT user_id, total_spend, n_orders FROM user_stats
  QUALIFY row_number() OVER (PARTITION BY user_id ORDER BY updated_at DESC) = 1
)
SELECT l.user_id, l.label_ts, l.churned, {col} AS feat
FROM labels l
{join} latest lt ON lt.user_id = l.user_id
