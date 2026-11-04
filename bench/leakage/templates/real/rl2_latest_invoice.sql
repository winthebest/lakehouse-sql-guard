-- leak: L2
-- param ord: inv.invoice_ts DESC | inv.ingested_at DESC, inv.invoice_id DESC
-- Leaky: the newest invoice of all time is picked for every cutoff, with no upper bound on its date or load time.
SELECT cl.customer_id, cl.cutoff_ts, cl.churned, inv.amount AS last_amount, inv.invoice_ts AS last_invoice_ts
FROM churn_labels AS cl
JOIN invoices AS inv ON inv.customer_id = cl.customer_id
QUALIFY ROW_NUMBER() OVER (PARTITION BY cl.customer_id, cl.cutoff_ts ORDER BY {ord}) = 1
