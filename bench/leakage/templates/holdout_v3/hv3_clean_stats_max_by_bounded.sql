-- leak: clean
SELECT l.user_id, l.label_ts, l.churned,
       max_by(s.total_spend, s.updated_at) AS lifetime_spend,
       max_by(s.n_orders, s.updated_at)    AS lifetime_orders,
       MAX(s.updated_at)                   AS stats_ts
FROM labels l
LEFT JOIN user_stats s
       ON s.user_id = l.user_id
      AND s.updated_at < l.label_ts
GROUP BY l.user_id, l.label_ts, l.churned
