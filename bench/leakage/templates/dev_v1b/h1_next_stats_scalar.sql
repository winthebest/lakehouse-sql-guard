-- leak: L1
-- param col: total_spend | n_orders
-- param gap: 0 | 2
-- Leaky: scalar subqueries read the first stats snapshot published after the prediction time (plus a gap), which did not exist yet.
SELECT sp.user_id, sp.label_ts, sp.churned,
       (SELECT min_by(st.{col}, st.updated_at) FROM user_stats AS st
        WHERE st.user_id = sp.user_id AND st.updated_at > sp.label_ts + INTERVAL '{gap} days') AS next_{col},
       (SELECT MIN(st.updated_at) FROM user_stats AS st
        WHERE st.user_id = sp.user_id AND st.updated_at > sp.label_ts + INTERVAL '{gap} days') AS next_stats_ts
FROM labels AS sp
