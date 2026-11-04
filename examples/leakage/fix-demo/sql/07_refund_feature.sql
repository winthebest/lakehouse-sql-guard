-- L6, explained only: refunds happen after the outcome.
SELECT
    l.customer_id,
    l.cutoff_ts,
    l.churned,
    SUM(r.refund_amount) AS refunds
FROM churn_labels l
LEFT JOIN refunds r ON r.customer_id = l.customer_id
GROUP BY l.customer_id, l.cutoff_ts, l.churned
