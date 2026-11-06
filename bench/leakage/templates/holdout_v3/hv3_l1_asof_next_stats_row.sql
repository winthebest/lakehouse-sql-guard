-- leak: L1
-- param col: total_spend | n_orders
-- ASOF with t < updated_at finds the nearest stats row AFTER t.
SELECT l.user_id, l.label_ts, l.churned,
       s.{col}                                     AS stats_value,
       date_diff('hour', l.label_ts, s.updated_at) AS hours_to_stats
FROM labels l
ASOF LEFT JOIN analytics.staging.stg_user_stats s
  ON s.user_id = l.user_id
 AND l.label_ts < s.updated_at
