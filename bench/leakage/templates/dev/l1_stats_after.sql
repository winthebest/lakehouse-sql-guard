-- leak: L1
-- param window: 1 | 3 | 7 | 14
SELECT l.user_id, l.label_ts, l.churned, s.n_orders AS orders_snapshot
FROM labels l
JOIN user_stats s ON s.user_id = l.user_id AND s.updated_at > l.label_ts + INTERVAL {window} DAY
QUALIFY row_number() OVER (PARTITION BY l.user_id, l.label_ts ORDER BY s.updated_at) = 1
