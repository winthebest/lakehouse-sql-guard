-- L3: counts every ticket the customer ever opened, including after the cutoff.
-- The INNER JOIN becomes a LEFT JOIN (so customers without tickets stay) and COUNT(*)
-- becomes COUNT(t.customer_id).
SELECT
    l.customer_id,
    l.cutoff_ts,
    l.churned,
    COUNT(*) AS tickets_total
FROM churn_labels l
JOIN support_tickets t ON t.customer_id = l.customer_id
GROUP BY l.customer_id, l.cutoff_ts, l.churned
