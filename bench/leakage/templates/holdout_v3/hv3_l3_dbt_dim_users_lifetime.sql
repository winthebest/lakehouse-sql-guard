-- leak: L3
WITH stg_entities AS (
    SELECT user_id, country, signup_ts FROM analytics.staging.stg_entities
),
stg_events AS (
    SELECT user_id, event_ts, amount FROM analytics.staging.stg_events
),
dim_users AS (
    SELECT en.user_id, en.country,
           COUNT(ev.amount)                               AS lifetime_orders,
           COALESCE(SUM(ev.amount), 0)                    AS lifetime_revenue,
           COUNT(DISTINCT date_trunc('month', ev.event_ts)) AS active_months
    FROM stg_entities en
    LEFT JOIN stg_events ev ON ev.user_id = en.user_id
    GROUP BY en.user_id, en.country
),
spine AS (
    SELECT user_id, label_ts, churned FROM analytics.marts.fct_training_spine
)
SELECT sp.user_id, sp.label_ts, sp.churned,
       du.country, du.lifetime_orders, du.active_months,
       CASE WHEN du.lifetime_revenue >= 500 THEN 'gold'
            WHEN du.lifetime_revenue >= 100 THEN 'silver'
            ELSE 'bronze' END AS value_tier
FROM spine sp
LEFT JOIN dim_users du ON du.user_id = sp.user_id
