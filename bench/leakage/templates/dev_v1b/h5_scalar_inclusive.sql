-- leak: L5
-- param col: total_spend | n_orders
-- Leaky: the scalar lookup includes the snapshot stamped exactly at the prediction time.
SELECT sp.user_id, sp.label_ts, sp.churned,
       (SELECT max_by(st.{col}, st.updated_at) FROM user_stats AS st
        WHERE st.user_id = sp.user_id AND st.updated_at <= sp.label_ts) AS feat
FROM labels AS sp
