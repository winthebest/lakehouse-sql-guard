-- leak: L7
-- param col: total_spend | n_orders
-- The snapshot is the newest one stamped before t, but its "staleness" is measured against
-- current_timestamp.
SELECT l.user_id, l.label_ts, l.churned,
       s.{col} AS stats_{col},
       date_diff('day', s.updated_at, CAST(current_timestamp AS TIMESTAMP)) AS stats_staleness_days
FROM labels l
ASOF LEFT JOIN user_stats s
  ON s.user_id = l.user_id
 AND l.label_ts > s.updated_at
