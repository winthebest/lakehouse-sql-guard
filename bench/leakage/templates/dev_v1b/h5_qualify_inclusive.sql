-- leak: L5
-- param col: st.total_spend | st.n_orders
-- Leaky: the latest-snapshot lookup uses <= on the update time, so a snapshot written at exactly the prediction instant is read.
SELECT sp.user_id, sp.label_ts, sp.churned, {col} AS feat, st.updated_at AS stats_ts
FROM labels AS sp
JOIN user_stats AS st ON st.user_id = sp.user_id AND st.updated_at <= sp.label_ts
QUALIFY ROW_NUMBER() OVER (PARTITION BY sp.user_id, sp.label_ts ORDER BY st.updated_at DESC) = 1
