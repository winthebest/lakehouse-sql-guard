-- leak: L5
-- param stats_rel: analytics.staging.stg_user_stats | user_stats
-- param col: total_spend | n_orders
-- Point-in-time pick with <= instead of <: at a midnight prediction time t, the recompute
-- stamped exactly t is taken, although it becomes known only just after t.
WITH spine AS (
    SELECT user_id, label_ts, churned
    FROM analytics.marts.fct_training_spine
),
stg_stats AS (
    SELECT user_id, updated_at AS snapshot_at, {col} AS v
    FROM {stats_rel}
),
int_stats_as_of AS (
    SELECT sp.user_id, sp.label_ts, st.v
    FROM spine sp
    JOIN stg_stats st
      ON st.user_id = sp.user_id
     AND st.snapshot_at <= sp.label_ts
    QUALIFY row_number() OVER (PARTITION BY sp.user_id, sp.label_ts ORDER BY st.snapshot_at DESC) = 1
)
SELECT sp.user_id, sp.label_ts, sp.churned,
       a.v AS {col}_as_of_t
FROM spine sp
LEFT JOIN int_stats_as_of a
  ON a.user_id = sp.user_id AND a.label_ts = sp.label_ts
