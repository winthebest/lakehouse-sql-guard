-- leak: L6
-- param agg: SUM | MAX
-- Leaky: the lateral subquery aggregates refunds recorded after the user churned.
SELECT lab.user_id, lab.label_ts, lab.churned, r.refund_total
FROM labels AS lab
LEFT JOIN LATERAL (
  SELECT {agg}(o.refund_after_churn) AS refund_total
  FROM user_outcomes AS o
  WHERE o.user_id = lab.user_id
) AS r ON TRUE
