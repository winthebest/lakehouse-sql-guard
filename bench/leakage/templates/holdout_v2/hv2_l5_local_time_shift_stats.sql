-- leak: L5
-- param offset_hours: 1 | 5 | 9
-- t is shifted to a "local" wall time (UTC + offset, less than a day) before it is compared
-- with the naive UTC stamp of the stats, so the next midnight recompute can slip in.
WITH spine_local AS (
    SELECT user_id, label_ts, churned,
           label_ts + INTERVAL {offset_hours} HOUR AS label_ts_local
    FROM labels
)
SELECT sl.user_id, sl.label_ts, sl.churned,
       max_by(s.total_spend, s.updated_at) AS spend_as_of_local_t,
       max_by(s.n_orders, s.updated_at)    AS orders_as_of_local_t
FROM spine_local sl
LEFT JOIN user_stats s
  ON s.user_id = sl.user_id
 AND s.updated_at < sl.label_ts_local
GROUP BY sl.user_id, sl.label_ts, sl.churned
