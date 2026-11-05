-- leak: L2
-- param col: total_spend | n_orders
-- "Latest row" via updated_at = MAX(updated_at) per user, computed over the whole table.
SELECT l.user_id, l.label_ts, l.churned,
       s.{col} AS latest_{col}
FROM labels l
LEFT JOIN (
    SELECT s.user_id, s.{col}
    FROM user_stats s
    WHERE s.updated_at = (SELECT MAX(s2.updated_at)
                          FROM user_stats s2
                          WHERE s2.user_id = s.user_id)
) s ON s.user_id = l.user_id
