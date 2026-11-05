-- leak: L1
-- param k: 1 | 2
-- param col: total_spend | n_orders
-- "Settled" stats: an equi-join on a computed key, the recompute k midnights after the day of
-- t starts. That midnight is always later than t (for a midnight t it is t + k days), so it
-- is a stats row from the future. The overnight delta is taken against the snapshot known at t.
SELECT l.user_id, l.label_ts, l.churned,
       settled.{col} AS settled_{col},
       settled.{col} - (SELECT max_by(s.{col}, s.updated_at)
                        FROM user_stats s
                        WHERE s.user_id = l.user_id
                          AND s.updated_at < l.label_ts) AS {col}_settling_delta
FROM labels l
LEFT JOIN user_stats settled
  ON settled.user_id = l.user_id
 AND settled.updated_at = date_trunc('day', l.label_ts) + INTERVAL {k} DAY
