-- leak: L4
-- param days: 90 | 180
-- Leaky: the window is bounded by invoice date only, so invoices that were issued before the cutoff but loaded after it (weekly batches, partner delay) are counted.
SELECT cl.customer_id, cl.cutoff_ts, cl.churned,
       SUM(inv.amount) AS spend_{days}d, COUNT(inv.invoice_id) AS n_{days}d
FROM churn_labels AS cl
LEFT JOIN invoices AS inv ON inv.customer_id = cl.customer_id
  AND inv.invoice_ts < cl.cutoff_ts
  AND inv.invoice_ts >= cl.cutoff_ts - INTERVAL '{days} days'
GROUP BY cl.customer_id, cl.cutoff_ts, cl.churned
