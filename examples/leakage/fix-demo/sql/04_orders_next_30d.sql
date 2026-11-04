-- L1: the window starts at the cutoff and runs forward. The fix mirrors it into the past.
SELECT
    l.customer_id,
    l.cutoff_ts,
    l.churned,
    SUM(o.amount) AS spend_30d
FROM churn_labels l
LEFT JOIN orders o
    ON o.customer_id = l.customer_id
    AND o.order_ts >= l.cutoff_ts
    AND o.order_ts < l.cutoff_ts + INTERVAL 30 DAY
GROUP BY l.customer_id, l.cutoff_ts, l.churned
