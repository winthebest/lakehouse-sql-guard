-- leak: L6
-- param expr: o.next_90d_spend | CAST(o.next_90d_spend > 0 AS INTEGER) | ln(1 + o.next_90d_spend)
-- Leaky: the next-90-day spend is the outcome that defines churn, observed only after the cutoff.
SELECT cl.customer_id, cl.cutoff_ts, cl.churned, {expr} AS feat
FROM churn_labels AS cl
LEFT JOIN customer_outcomes AS o USING (customer_id, cutoff_ts)
