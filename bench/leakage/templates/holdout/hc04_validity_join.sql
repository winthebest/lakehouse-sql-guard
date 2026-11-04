-- leak: clean
-- param col: v.total_spend | v.n_orders
-- param join: JOIN | LEFT JOIN
-- Clean: SCD2-style validity ranges built from update times; the prediction time must fall strictly after the version start, so the version in force just before it is read.
WITH versions AS (
  SELECT user_id, total_spend, n_orders, updated_at AS valid_from,
         LEAD(updated_at) OVER (PARTITION BY user_id ORDER BY updated_at) AS valid_to
  FROM user_stats
)
SELECT sp.user_id, sp.label_ts, sp.churned, {col} AS feat, v.valid_from AS stats_as_of
FROM labels AS sp
{join} versions AS v ON v.user_id = sp.user_id
  AND sp.label_ts > v.valid_from
  AND (sp.label_ts <= v.valid_to OR v.valid_to IS NULL)
