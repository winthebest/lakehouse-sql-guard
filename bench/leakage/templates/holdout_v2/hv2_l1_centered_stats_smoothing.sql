-- leak: L1
-- param k: 1 | 3
-- param col: total_spend | n_orders
-- A centred moving average (FOLLOWING) on the snapshot picked before t pulls in the next
-- k snapshots, which are stamped at or after t.
WITH smoothed AS (
    SELECT user_id, updated_at,
           AVG({col}) OVER (PARTITION BY user_id ORDER BY updated_at
                            ROWS BETWEEN {k} PRECEDING AND {k} FOLLOWING) AS v
    FROM user_stats
)
SELECT l.user_id, l.label_ts, l.churned,
       s.v AS {col}_centered_ma
FROM labels l
LEFT JOIN smoothed s
  ON s.user_id = l.user_id
 AND s.updated_at < l.label_ts
QUALIFY row_number() OVER (PARTITION BY l.user_id, l.label_ts ORDER BY s.updated_at DESC) = 1
