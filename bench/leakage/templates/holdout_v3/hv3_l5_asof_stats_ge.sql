-- leak: L5
-- param col: total_spend | n_orders
SELECT l.user_id, l.label_ts, l.churned,
       s.{col}                                     AS stats_value,
       date_diff('hour', s.updated_at, l.label_ts) AS stats_age_hours
FROM labels l
ASOF LEFT JOIN user_stats s
  ON s.user_id = l.user_id
 AND l.label_ts >= s.updated_at
