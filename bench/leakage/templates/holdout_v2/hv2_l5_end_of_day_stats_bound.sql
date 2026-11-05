-- leak: L5
-- param col: total_spend | n_orders
-- Upper bound at the end of the day of t (date_trunc + 1 day, exclusive). For a midnight t
-- this takes the recompute stamped exactly t.
SELECT l.user_id, l.label_ts, l.churned,
       (SELECT max_by(s.{col}, s.updated_at)
        FROM user_stats s
        WHERE s.user_id = l.user_id
          AND s.updated_at < date_trunc('day', l.label_ts) + INTERVAL 1 DAY) AS {col}_end_of_day
FROM labels l
