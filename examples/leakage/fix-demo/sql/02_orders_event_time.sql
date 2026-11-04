-- L4: bounds event time only. Orders for the past that load late still leak in; the fix
-- adds the load-time bound next to it.
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
