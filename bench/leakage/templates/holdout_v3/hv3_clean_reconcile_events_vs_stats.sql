-- leak: clean
WITH loaded_spend AS (
    SELECT l.user_id, l.label_ts, l.churned,
           COALESCE(SUM(e.amount), 0) AS spend_loaded,
           COUNT(e.event_id)          AS orders_loaded
    FROM labels l
    LEFT JOIN events e
           ON e.user_id = l.user_id
          AND e.ingested_at < l.label_ts
    GROUP BY l.user_id, l.label_ts, l.churned
)
SELECT ls.user_id, ls.label_ts, ls.churned,
       ls.spend_loaded,
       ls.spend_loaded - s.total_spend AS spend_since_recompute,
       ls.orders_loaded - s.n_orders   AS orders_since_recompute
FROM loaded_spend ls
ASOF LEFT JOIN user_stats s
  ON s.user_id = ls.user_id
 AND ls.label_ts > s.updated_at
