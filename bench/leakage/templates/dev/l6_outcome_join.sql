-- leak: L6
-- param col: o.refund_after_churn | o.days_to_churn | coalesce(o.refund_after_churn, 0)
-- param join: JOIN | LEFT JOIN
SELECT l.user_id, l.label_ts, l.churned, {col} AS feat
FROM labels l
{join} user_outcomes o ON o.user_id = l.user_id AND o.label_ts = l.label_ts
