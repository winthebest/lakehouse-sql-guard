-- leak: clean
-- param col: st.total_spend | st.n_orders | st.total_spend / NULLIF(st.n_orders, 0)
-- Clean: stats are taken only from a strictly earlier calendar day than the prediction, which is never later than the true visibility point.
SELECT sp.user_id, sp.label_ts, sp.churned, {col} AS feat, st.updated_at AS stats_ts
FROM labels AS sp
JOIN user_stats AS st ON st.user_id = sp.user_id AND CAST(st.updated_at AS DATE) < CAST(sp.label_ts AS DATE)
QUALIFY ROW_NUMBER() OVER (PARTITION BY sp.user_id, sp.label_ts ORDER BY st.updated_at DESC) = 1
