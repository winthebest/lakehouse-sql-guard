-- leak: L3
-- param col: total_spend | n_orders
-- param fn: AVG | STDDEV_SAMP | MEDIAN
-- An intermediate "user profile" model aggregates every snapshot of the user, including
-- recomputes stamped after t.
WITH stg_user_stats AS (
    SELECT user_id, updated_at AS snapshot_at, CAST({col} AS DOUBLE) AS v
    FROM analytics.staging.stg_user_stats
),
int_user_profile AS (
    SELECT user_id, {fn}(v) AS v_profile, COUNT(*) AS n_snapshots
    FROM stg_user_stats
    GROUP BY user_id
),
spine AS (
    SELECT user_id, label_ts, churned
    FROM analytics.marts.fct_training_spine
)
SELECT sp.user_id, sp.label_ts, sp.churned,
       p.v_profile   AS {col}_profile,
       p.n_snapshots
FROM spine sp
LEFT JOIN int_user_profile p ON p.user_id = sp.user_id
