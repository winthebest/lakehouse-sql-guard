-- leak: L3
SELECT l.user_id, l.label_ts, l.churned,
       AVG(s.n_orders)                     AS avg_orders_over_history,
       COUNT(s.updated_at)                 AS n_recomputes,
       MAX(s.total_spend) - MIN(s.total_spend) AS spend_range
FROM labels l
LEFT JOIN staging.stg_user_stats s ON s.user_id = l.user_id
GROUP BY ALL
