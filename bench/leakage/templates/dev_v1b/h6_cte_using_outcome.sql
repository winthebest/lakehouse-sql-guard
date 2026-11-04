-- leak: L6
-- param expr: fu.days_to_churn | COALESCE(fu.refund_after_churn, 0) > 0 | ln(1 + COALESCE(fu.refund_after_churn, 0)) | fu.days_to_churn <= 7
-- Leaky: a wrapper CTE over the outcomes table is joined on the key columns and a function of the post-churn outcome becomes the feature.
WITH fu AS (
  SELECT user_id, label_ts, refund_after_churn, days_to_churn FROM user_outcomes
)
SELECT lab.user_id, lab.label_ts, lab.churned, {expr} AS feat
FROM labels AS lab
LEFT JOIN fu USING (user_id, label_ts)
