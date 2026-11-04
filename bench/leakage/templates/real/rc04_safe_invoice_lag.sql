-- leak: clean
-- param lag: 15 | 21
-- Clean: only invoices older than the longest loading delay in this feed (weekly batch plus partner week, about 14 days) are used, so they had landed without an ingestion filter.
SELECT cl.customer_id, cl.cutoff_ts, cl.churned, SUM(inv.amount) AS spend_lagged
FROM churn_labels AS cl
LEFT JOIN invoices AS inv ON inv.customer_id = cl.customer_id
  AND inv.invoice_ts < cl.cutoff_ts - INTERVAL '{lag} days'
  AND inv.invoice_ts >= cl.cutoff_ts - INTERVAL '180 days'
GROUP BY cl.customer_id, cl.cutoff_ts, cl.churned
