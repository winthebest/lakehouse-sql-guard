-- leak: L5
-- param agg: SUM | COUNT
-- Leaky: <= on the load time admits the Monday-midnight batch that lands at the same instant as a cutoff falling on that Monday.
SELECT cl.customer_id, cl.cutoff_ts, cl.churned, {agg}(inv.amount) AS amt_180d
FROM churn_labels AS cl
LEFT JOIN invoices AS inv ON inv.customer_id = cl.customer_id
  AND inv.ingested_at <= cl.cutoff_ts
  AND inv.invoice_ts >= cl.cutoff_ts - INTERVAL '180 days'
GROUP BY cl.customer_id, cl.cutoff_ts, cl.churned
