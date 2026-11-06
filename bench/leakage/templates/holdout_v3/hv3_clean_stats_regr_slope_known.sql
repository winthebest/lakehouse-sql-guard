-- leak: clean
-- param n: 14 | 28
SELECT l.user_id, l.label_ts, l.churned,
       regr_slope(s.total_spend, epoch(s.updated_at) / 86400.0) AS spend_per_day_trend,
       regr_slope(s.n_orders,    epoch(s.updated_at) / 86400.0) AS orders_per_day_trend
FROM labels l
LEFT JOIN analytics.staging.stg_user_stats s
       ON s.user_id = l.user_id
      AND s.updated_at < l.label_ts
      AND s.updated_at >= l.label_ts - INTERVAL {n} DAY
GROUP BY l.user_id, l.label_ts, l.churned
