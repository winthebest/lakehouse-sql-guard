-- leak: L1
-- param days: 30 | 90
-- Leaky: the aggregate covers invoices dated after the cutoff, which is exactly the churn outcome window.
SELECT cl.customer_id, cl.cutoff_ts, cl.churned,
       COUNT(inv.invoice_id) AS n_next_{days}d, MIN(inv.invoice_ts) AS next_invoice_ts
FROM churn_labels AS cl
LEFT JOIN invoices AS inv ON inv.customer_id = cl.customer_id
  AND inv.invoice_ts >= cl.cutoff_ts
  AND inv.invoice_ts < cl.cutoff_ts + INTERVAL '{days} days'
GROUP BY cl.customer_id, cl.cutoff_ts, cl.churned
