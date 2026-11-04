-- Looks correct, but bounds event time only: orders for the past that load late
-- still leak in. Static analysis flags it; replay over snapshot history confirms it.
SELECT
    l.customer_id,
    l.cutoff_ts,
    l.churned,
    SUM(o.amount) AS spend_before_cutoff
FROM churn_labels l
LEFT JOIN orders o
    ON o.customer_id = l.customer_id
    AND o.order_ts < l.cutoff_ts
GROUP BY l.customer_id, l.cutoff_ts, l.churned
