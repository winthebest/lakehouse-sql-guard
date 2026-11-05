-- leak: clean
-- param col: total_spend | n_orders
-- param join: ASOF LEFT JOIN | ASOF JOIN
-- ASOF picks the newest stats row with updated_at strictly before label_ts; a recompute
-- stamped exactly t is excluded, matching the strict boundary.
SELECT l.user_id, l.label_ts, l.churned,
       s.{col} AS stats_{col},
       date_diff('hour', s.updated_at, l.label_ts) AS stats_age_hours
FROM labels l
{join} user_stats s
  ON s.user_id = l.user_id
 AND l.label_ts > s.updated_at
