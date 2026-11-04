-- leak: clean
-- param region: cu.country | CASE WHEN cu.country = 'United Kingdom' THEN 'home' ELSE 'abroad' END
-- Clean: static country attribute joined to a CTE of invoices that were loaded before each cutoff.
WITH recent AS (
  SELECT cl.customer_id, cl.cutoff_ts, SUM(inv.amount) AS spend_60d, AVG(inv.n_lines) AS avg_lines
  FROM churn_labels AS cl
  JOIN invoices AS inv ON inv.customer_id = cl.customer_id
  WHERE inv.ingested_at < cl.cutoff_ts AND inv.invoice_ts >= cl.cutoff_ts - INTERVAL '60 days'
  GROUP BY cl.customer_id, cl.cutoff_ts
)
SELECT cl.customer_id, cl.cutoff_ts, cl.churned, {region} AS region, r.spend_60d, r.avg_lines
FROM churn_labels AS cl
JOIN customers AS cu USING (customer_id)
LEFT JOIN recent AS r ON r.customer_id = cl.customer_id AND r.cutoff_ts = cl.cutoff_ts
