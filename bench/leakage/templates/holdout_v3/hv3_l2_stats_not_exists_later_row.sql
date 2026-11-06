-- leak: L2
SELECT l.user_id, l.label_ts, l.churned,
       s.total_spend AS lifetime_spend,
       s.n_orders    AS lifetime_orders
FROM labels l
LEFT JOIN (
    SELECT s.*
    FROM user_stats s
    WHERE NOT EXISTS (SELECT 1 FROM user_stats s2
                       WHERE s2.user_id = s.user_id
                         AND s2.updated_at > s.updated_at)
) s ON s.user_id = l.user_id
