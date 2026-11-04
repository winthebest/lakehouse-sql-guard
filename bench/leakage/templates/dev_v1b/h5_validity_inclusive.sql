-- leak: L5
-- param col: v.total_spend | v.n_orders
-- param cond: sp.label_ts >= v.valid_from AND (sp.label_ts < v.valid_to OR v.valid_to IS NULL) | sp.label_ts BETWEEN v.valid_from AND COALESCE(v.valid_to, TIMESTAMP '2999-01-01')
-- Leaky: the validity range includes its start instant, so a snapshot published at exactly the prediction time is used.
WITH versions AS (
  SELECT user_id, total_spend, n_orders, updated_at AS valid_from,
         LEAD(updated_at) OVER (PARTITION BY user_id ORDER BY updated_at) AS valid_to
  FROM user_stats
)
SELECT sp.user_id, sp.label_ts, sp.churned, {col} AS feat
FROM labels AS sp
JOIN versions AS v ON v.user_id = sp.user_id AND {cond}
