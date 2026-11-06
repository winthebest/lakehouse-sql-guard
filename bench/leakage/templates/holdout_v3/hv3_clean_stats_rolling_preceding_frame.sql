-- leak: clean
-- param n: 3 | 7
WITH stats_rolling AS (
    SELECT user_id, updated_at,
           AVG(total_spend) OVER w             AS avg_spend_rolling,
           MAX(n_orders) OVER w - MIN(n_orders) OVER w AS orders_added_rolling
    FROM user_stats
    WINDOW w AS (PARTITION BY user_id ORDER BY updated_at ROWS BETWEEN {n} PRECEDING AND CURRENT ROW)
)
SELECT l.user_id, l.label_ts, l.churned, r.avg_spend_rolling, r.orders_added_rolling
FROM labels l
ASOF LEFT JOIN stats_rolling r
  ON r.user_id = l.user_id
 AND l.label_ts > r.updated_at
