-- leak: L1
WITH stats_next AS (
    SELECT user_id, updated_at,
           LEAD(total_spend) OVER w - total_spend AS spend_next_recompute,
           LEAD(n_orders)    OVER w - n_orders    AS orders_next_recompute
    FROM user_stats
    WINDOW w AS (PARTITION BY user_id ORDER BY updated_at)
)
SELECT l.user_id, l.label_ts, l.churned, n.spend_next_recompute, n.orders_next_recompute
FROM labels l
ASOF LEFT JOIN stats_next n
  ON n.user_id = l.user_id
 AND l.label_ts > n.updated_at
