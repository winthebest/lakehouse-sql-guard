-- leak: L5
-- param max_age_h: 24 | 48
-- param col: total_spend | n_orders
-- A freshness band on the integer hour age of the snapshot, date_diff('hour', updated_at, t)
-- BETWEEN 0 AND max_age_h. Age 0 includes a recompute stamped exactly t (at a midnight t),
-- which is not known at t. Later midnights have a negative age and are excluded.
SELECT l.user_id, l.label_ts, l.churned,
       max_by(s.{col}, s.updated_at)                      AS fresh_{col},
       MIN(date_diff('hour', s.updated_at, l.label_ts))   AS freshest_age_hours
FROM labels l
LEFT JOIN user_stats s
  ON s.user_id = l.user_id
 AND date_diff('hour', s.updated_at, l.label_ts) BETWEEN 0 AND {max_age_h}
GROUP BY l.user_id, l.label_ts, l.churned
