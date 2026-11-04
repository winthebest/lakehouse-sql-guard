-- L3, explained only: the total is computed per customer in a CTE that cannot see the cutoff.
-- No local rewrite exists; join `orders` to the labels and bound it there.
WITH spend AS (
    SELECT customer_id, SUM(amount) AS total_spend FROM orders GROUP BY customer_id
)
SELECT l.customer_id, l.cutoff_ts, l.churned, s.total_spend
FROM churn_labels l
LEFT JOIN spend s ON s.customer_id = l.customer_id
