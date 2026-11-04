-- L5: an order loaded exactly at the cutoff is not yet known when the prediction is made.
SELECT
    l.customer_id,
    l.cutoff_ts,
    l.churned,
    COUNT(o.order_id) AS orders_30d
FROM churn_labels l
LEFT JOIN orders o
    ON o.customer_id = l.customer_id
    AND o.loaded_at <= l.cutoff_ts
    AND o.order_ts >= l.cutoff_ts - INTERVAL 30 DAY
GROUP BY l.customer_id, l.cutoff_ts, l.churned
