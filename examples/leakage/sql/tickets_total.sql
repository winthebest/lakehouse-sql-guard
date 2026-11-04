-- Leaky: counts every ticket the customer ever opened, including after the cutoff.
SELECT
    l.customer_id,
    l.cutoff_ts,
    l.churned,
    COUNT(t.ticket_id) AS tickets_total,
    c.country
FROM churn_labels l
JOIN customers c ON c.customer_id = l.customer_id
LEFT JOIN support_tickets t ON t.customer_id = l.customer_id
GROUP BY l.customer_id, l.cutoff_ts, l.churned, c.country
