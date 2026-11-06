-- leak: L5
WITH stg_stats AS (
    SELECT user_id, updated_at, CAST(updated_at AS DATE) AS stats_date, total_spend, n_orders
    FROM analytics.staging.stg_user_stats
),
spine AS (
    SELECT user_id, label_ts, CAST(label_ts AS DATE) AS label_date, churned
    FROM analytics.marts.fct_training_spine
),
int_daily_stats AS (
    SELECT sp.user_id, sp.label_ts, sp.churned, st.total_spend, st.n_orders
    FROM spine sp
    LEFT JOIN stg_stats st
           ON st.user_id = sp.user_id
          AND st.stats_date = sp.label_date
)
SELECT * FROM int_daily_stats
