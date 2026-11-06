-- leak: clean
WITH timeline AS (
    SELECT user_id, updated_at AS ts, 1 AS kind, updated_at AS stats_ts,
           CAST(NULL AS TIMESTAMP) AS label_ts, CAST(NULL AS BIGINT) AS churned
    FROM staging.stg_user_stats
    UNION ALL BY NAME
    SELECT user_id, label_ts AS ts, 0 AS kind, label_ts, churned
    FROM marts.fct_training_spine
),
filled AS (
    SELECT *,
           MAX(stats_ts) OVER (PARTITION BY user_id ORDER BY ts, kind
                               ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS last_stats_ts
    FROM timeline
)
SELECT f.user_id, f.label_ts, f.churned,
       date_diff('hour', f.last_stats_ts, f.label_ts) AS stats_age_hours,
       s.n_orders                                     AS n_orders_known
FROM filled f
LEFT JOIN staging.stg_user_stats s
       ON s.user_id = f.user_id
      AND s.updated_at = f.last_stats_ts
WHERE f.kind = 0
