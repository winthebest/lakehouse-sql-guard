-- leak: L7
-- param days: 30 | 90 | 365
-- dim_users computes "is new user" against current_date; the mart joins it to the spine.
WITH stg_entities AS (
    SELECT user_id, country, signup_ts
    FROM analytics.staging.stg_entities
),
dim_users AS (
    SELECT user_id, country,
           signup_ts > current_date - INTERVAL {days} DAY AS is_new_user,
           date_trunc('month', signup_ts)                AS signup_cohort
    FROM stg_entities
),
spine AS (
    SELECT user_id, label_ts, churned
    FROM marts.fct_training_spine
)
SELECT sp.user_id, sp.label_ts, sp.churned,
       d.country, d.is_new_user, d.signup_cohort
FROM spine sp
LEFT JOIN dim_users d ON d.user_id = sp.user_id
