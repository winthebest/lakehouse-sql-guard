-- leak: L2
-- param pick: arg_max | max_by
-- param col: total_spend | n_orders
-- Leaky: the value at the latest stats update of all time is read with no upper bound on the update time.
SELECT sp.user_id, sp.label_ts, sp.churned,
       (SELECT {pick}(st.{col}, st.updated_at) FROM user_stats AS st WHERE st.user_id = sp.user_id) AS latest_{col}
FROM labels AS sp
