-- leak: clean
-- param lookback: 90 | 180
-- Clean: the lateral subquery is restricted to invoices loaded before the cutoff, so the last-invoice timestamp is one that was visible then.
SELECT cl.customer_id, cl.cutoff_ts, cl.churned, r.last_invoice_ts, r.n_recent
FROM churn_labels AS cl
LEFT JOIN LATERAL (
  SELECT MAX(inv.invoice_ts) AS last_invoice_ts, COUNT(*) AS n_recent
  FROM invoices AS inv
  WHERE inv.customer_id = cl.customer_id
    AND inv.ingested_at < cl.cutoff_ts
    AND inv.invoice_ts >= cl.cutoff_ts - INTERVAL '{lookback} days'
) AS r ON TRUE
