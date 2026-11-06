-- leak: clean
-- dbt-style layering: staging renames, point-in-time model, mart.
WITH spine AS (
    SELECT user_id, label_ts, churned
    FROM analytics.marts.fct_training_spine
),
stg_stats AS (
    SELECT user_id,
           updated_at  AS stats_ts,
           total_spend AS lifetime_spend,
           n_orders    AS lifetime_orders
    FROM analytics.staging.stg_user_stats
),
int_stats_pit AS (
    SELECT sp.user_id, sp.label_ts, sp.churned, st.lifetime_spend, st.lifetime_orders
    FROM spine sp
    LEFT JOIN stg_stats st
           ON st.user_id = sp.user_id
          AND st.stats_ts < sp.label_ts
    QUALIFY row_number() OVER (PARTITION BY sp.user_id, sp.label_ts
                               ORDER BY st.stats_ts DESC NULLS LAST) = 1
)
SELECT user_id, label_ts, churned,
       lifetime_spend,
       lifetime_orders,
       lifetime_spend / NULLIF(lifetime_orders, 0) AS avg_order_value
FROM int_stats_pit
