-- leak: L3
WITH user_trend AS (
    SELECT user_id,
           regr_slope(total_spend, epoch(updated_at) / 86400.0) AS spend_per_day_trend,
           regr_slope(n_orders,    epoch(updated_at) / 86400.0) AS orders_per_day_trend
    FROM user_stats
    GROUP BY user_id
)
SELECT l.user_id, l.label_ts, l.churned, ut.spend_per_day_trend, ut.orders_per_day_trend
FROM labels l
LEFT JOIN user_trend ut ON ut.user_id = l.user_id
