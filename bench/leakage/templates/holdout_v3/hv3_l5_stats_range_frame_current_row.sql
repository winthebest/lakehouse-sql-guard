-- leak: L5
WITH timeline AS (
    SELECT user_id, updated_at AS ts, updated_at AS stats_ts,
           CAST(NULL AS TIMESTAMP) AS label_ts, CAST(NULL AS BIGINT) AS churned
    FROM user_stats
    UNION ALL
    SELECT user_id, label_ts AS ts, CAST(NULL AS TIMESTAMP) AS stats_ts, label_ts, churned
    FROM labels
),
filled AS (
    SELECT *,
           MAX(stats_ts) OVER (PARTITION BY user_id ORDER BY ts
                               RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS last_stats_ts
    FROM timeline
)
SELECT f.user_id, f.label_ts, f.churned,
       date_diff('hour', f.last_stats_ts, f.label_ts) AS stats_age_hours,
       s.n_orders                                     AS n_orders_known
FROM filled f
LEFT JOIN user_stats s
       ON s.user_id = f.user_id
      AND s.updated_at = f.last_stats_ts
WHERE f.label_ts IS NOT NULL
