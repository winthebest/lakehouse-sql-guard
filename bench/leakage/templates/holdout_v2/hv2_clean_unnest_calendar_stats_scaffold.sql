-- leak: clean
-- param days: 3 | 7
-- param col: total_spend | n_orders
-- A calendar scaffold of the midnights before t (UNNEST of generate_series), equi-joined to
-- the stats recompute of each midnight. The scaffold includes the midnight of t's own day,
-- so the WHERE keeps only midnights strictly before t: at a midnight t, the recompute
-- stamped t is not known yet.
WITH scaffold AS (
    SELECT l.user_id, l.label_ts, l.churned,
           unnest(generate_series(date_trunc('day', l.label_ts) - INTERVAL {days} DAY,
                                  date_trunc('day', l.label_ts),
                                  INTERVAL 1 DAY)) AS midnight
    FROM labels l
),
daily AS (
    SELECT sc.user_id, sc.label_ts, sc.churned, sc.midnight, s.{col} AS v
    FROM scaffold sc
    LEFT JOIN user_stats s
      ON s.user_id = sc.user_id
     AND s.updated_at = sc.midnight
    WHERE sc.midnight < sc.label_ts
)
SELECT user_id, label_ts, churned,
       MAX(v) - MIN(v) AS {col}_range_{days}d,
       COUNT(v)        AS n_snapshots_seen
FROM daily
GROUP BY user_id, label_ts, churned
