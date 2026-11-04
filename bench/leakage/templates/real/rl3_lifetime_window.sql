-- leak: L3
-- param fn: SUM(amount) | COUNT(*) | AVG(amount)
-- Leaky: a lifetime figure over every invoice of the customer is joined to each cutoff, including invoices after it.
WITH life AS (
  SELECT DISTINCT customer_id, {fn} OVER (PARTITION BY customer_id) AS lifetime FROM invoices
)
SELECT cl.customer_id, cl.cutoff_ts, cl.churned, lf.lifetime
FROM churn_labels AS cl
LEFT JOIN life AS lf USING (customer_id)
