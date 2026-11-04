-- leak: clean
-- param days: 30 | 60
-- Clean: the activity flag only considers invoices loaded before the cutoff.
SELECT cl.customer_id, cl.cutoff_ts, cl.churned,
       CASE WHEN EXISTS (
         SELECT 1 FROM invoices AS inv
         WHERE inv.customer_id = cl.customer_id
           AND inv.ingested_at < cl.cutoff_ts
           AND inv.invoice_ts >= cl.cutoff_ts - INTERVAL '{days} days'
       ) THEN 1 ELSE 0 END AS bought_last_{days}d
FROM churn_labels AS cl
