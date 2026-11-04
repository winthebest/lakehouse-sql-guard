-- leak: L2
-- param spec: LAST_VALUE(total_spend) OVER (PARTITION BY user_id ORDER BY updated_at ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) | FIRST_VALUE(total_spend) OVER (PARTITION BY user_id ORDER BY updated_at DESC)
-- Leaky: the per-user latest value is computed over the whole stats history and then joined to predictions by user only.
WITH cur AS (
  SELECT DISTINCT user_id, {spec} AS cur_spend FROM user_stats
)
SELECT lab.user_id, lab.label_ts, lab.churned, c.cur_spend
FROM labels AS lab
LEFT JOIN cur AS c USING (user_id)
