-- leak: L6
SELECT user_id, label_ts, churned,
       LAG(churned) OVER (PARTITION BY user_id ORDER BY label_ts) AS prev_churned,
       SUM(churned) OVER (PARTITION BY user_id ORDER BY label_ts
                          ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS prior_churns
FROM analytics.marts.fct_training_spine
