-- leak: L5
-- param eps: 1 SECOND | 12 HOUR
SELECT l.user_id, l.label_ts, l.churned, s.total_spend, s.n_orders
FROM labels l
LEFT JOIN user_stats s
       ON s.user_id = l.user_id
      AND s.updated_at < l.label_ts + INTERVAL {eps}
QUALIFY row_number() OVER (PARTITION BY l.user_id, l.label_ts
                           ORDER BY s.updated_at DESC NULLS LAST) = 1
