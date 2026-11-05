-- leak: clean
-- param stats_rel: analytics.staging.stg_user_stats | staging.stg_user_stats
-- param lag_days: 0 | 1 | 7
-- dbt layering: staging rename, intermediate point-in-time pick, mart. The chosen snapshot
-- has snapshot_at < label_ts - lag_days <= label_ts, so it is known at t.
WITH spine AS (
    SELECT user_id, label_ts, churned
    FROM analytics.marts.fct_training_spine
),
stg_stats AS (
    SELECT user_id,
           updated_at  AS snapshot_at,
           total_spend AS lifetime_spend,
           n_orders    AS lifetime_orders
    FROM {stats_rel}
),
int_stats_as_of AS (
    SELECT sp.user_id, sp.label_ts, st.lifetime_spend, st.lifetime_orders
    FROM spine sp
    JOIN stg_stats st
      ON st.user_id = sp.user_id
     AND st.snapshot_at < sp.label_ts - INTERVAL {lag_days} DAY
    QUALIFY row_number() OVER (PARTITION BY sp.user_id, sp.label_ts
                               ORDER BY st.snapshot_at DESC) = 1
)
SELECT sp.user_id, sp.label_ts, sp.churned,
       a.lifetime_spend,
       a.lifetime_orders,
       a.lifetime_spend / NULLIF(a.lifetime_orders, 0) AS avg_order_value
FROM spine sp
LEFT JOIN int_stats_as_of a
  ON a.user_id = sp.user_id AND a.label_ts = sp.label_ts
