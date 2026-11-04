-- leak: L6
-- param agg: AVG | SUM
-- Leaky: the per-country churn rate is a window over the label column itself, covering this row and all later cutoffs.
SELECT cl.customer_id, cl.cutoff_ts, cl.churned,
       {agg}(cl.churned) OVER (PARTITION BY cu.country) AS country_churn
FROM churn_labels AS cl
JOIN customers AS cu ON cu.customer_id = cl.customer_id
