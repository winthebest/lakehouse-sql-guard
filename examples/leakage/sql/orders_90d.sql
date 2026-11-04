-- Point-in-time correct: only orders already loaded before the cutoff, last 90 days.
SELECT
    l.customer_id,
    l.cutoff_ts,
    l.churned,
    COUNT(o.order_id) AS orders_90d,
    COALESCE(SUM(o.amount), 0) AS spend_90d
FROM churn_labels l
LEFT JOIN orders o
    ON o.customer_id = l.customer_id
    AND o.loaded_at < l.cutoff_ts
    AND o.order_ts >= l.cutoff_ts - INTERVAL 90 DAY
GROUP BY l.customer_id, l.cutoff_ts, l.churned
