-- leak: L2
-- param col: v.total_spend | v.n_orders
-- Leaky: the open-ended (current) version of each stats row is joined to every prediction, regardless of when the version started.
WITH versions AS (
  SELECT user_id, total_spend, n_orders, updated_at AS valid_from,
         LEAD(updated_at) OVER (PARTITION BY user_id ORDER BY updated_at) AS valid_to
  FROM user_stats
)
SELECT sp.user_id, sp.label_ts, sp.churned, {col} AS feat, v.valid_from AS stats_as_of
FROM labels AS sp
LEFT JOIN versions AS v ON v.user_id = sp.user_id AND v.valid_to IS NULL
