-- leak: clean
-- param n: 7 | 14
SELECT l.user_id, l.label_ts, l.churned,
       AVG(s.n_orders)           AS avg_orders_last_recomputes,
       stddev_samp(s.total_spend) AS sd_spend_last_recomputes,
       COUNT(s.updated_at)       AS n_recomputes
FROM labels l
LEFT JOIN user_stats s
       ON s.user_id = l.user_id
      AND s.updated_at < l.label_ts
      AND s.updated_at >= l.label_ts - INTERVAL {n} DAY
GROUP BY ALL
