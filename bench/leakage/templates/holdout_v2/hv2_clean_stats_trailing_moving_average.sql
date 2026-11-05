-- leak: clean
-- param k: 2 | 6
-- param col: total_spend | n_orders
-- The moving average looks only backwards (PRECEDING .. CURRENT ROW); the row picked has
-- updated_at < t, so every row in its frame is stamped before t.
WITH stats_smoothed AS (
    SELECT user_id, updated_at,
           AVG({col}) OVER (PARTITION BY user_id ORDER BY updated_at
                            ROWS BETWEEN {k} PRECEDING AND CURRENT ROW) AS ma
    FROM user_stats
)
SELECT l.user_id, l.label_ts, l.churned,
       s.ma AS {col}_trailing_ma{k}
FROM labels l
LEFT JOIN stats_smoothed s
  ON s.user_id = l.user_id
 AND s.updated_at < l.label_ts
QUALIFY row_number() OVER (PARTITION BY l.user_id, l.label_ts ORDER BY s.updated_at DESC) = 1
