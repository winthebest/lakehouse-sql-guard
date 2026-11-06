-- leak: L2
WITH stg_user_stats AS (
    SELECT user_id, updated_at, total_spend, n_orders
    FROM analytics.staging.stg_user_stats
),
int_latest_stats AS (
    SELECT *
    FROM stg_user_stats
    QUALIFY row_number() OVER (PARTITION BY user_id ORDER BY updated_at DESC) = 1
),
spine AS (
    SELECT user_id, label_ts, churned FROM analytics.marts.fct_training_spine
)
SELECT sp.user_id, sp.label_ts, sp.churned,
       ls.total_spend AS lifetime_spend,
       ls.n_orders    AS lifetime_orders
FROM spine sp
LEFT JOIN int_latest_stats ls ON ls.user_id = sp.user_id
