-- leak: clean
-- param pick: arg_max | max_by
-- param col: total_spend | n_orders
-- Clean: scalar subquery picks the value at the latest stats update strictly before the prediction time.
SELECT sp.user_id, sp.label_ts, sp.churned,
       (SELECT {pick}(st.{col}, st.updated_at)
        FROM user_stats AS st
        WHERE st.user_id = sp.user_id AND st.updated_at < sp.label_ts) AS latest_{col}
FROM labels AS sp
