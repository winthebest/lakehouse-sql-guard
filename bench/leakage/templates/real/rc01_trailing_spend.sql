-- leak: clean
-- param days: 30 | 90 | 180
-- Clean: only invoices already loaded (ingested_at before the cutoff) inside a bounded invoice-time window feed the aggregate.
SELECT cl.customer_id, cl.cutoff_ts, cl.churned,
       COALESCE(SUM(inv.amount), 0) AS spend_{days}d, COUNT(inv.invoice_id) AS n_invoices_{days}d
FROM churn_labels AS cl
LEFT JOIN invoices AS inv ON inv.customer_id = cl.customer_id
  AND inv.ingested_at < cl.cutoff_ts
  AND inv.invoice_ts >= cl.cutoff_ts - INTERVAL '{days} days'
GROUP BY cl.customer_id, cl.cutoff_ts, cl.churned
