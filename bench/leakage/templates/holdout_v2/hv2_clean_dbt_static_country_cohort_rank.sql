-- leak: clean
-- param fn: percent_rank() | cume_dist()
-- param unit: day | week
-- Whole-table window aggregates, but over entities only: a static table that is always known,
-- so ranking a user's signup within the whole country cohort uses nothing unknown at t.
WITH stg_entities AS (
    SELECT user_id, country, signup_ts
    FROM analytics.staging.stg_entities
),
int_country_cohorts AS (
    SELECT user_id, country, signup_ts,
           {fn} OVER (PARTITION BY country ORDER BY signup_ts) AS signup_rank_in_country,
           COUNT(*) OVER (PARTITION BY country)                AS country_size
    FROM stg_entities
),
spine AS (
    SELECT user_id, label_ts, churned
    FROM analytics.marts.fct_training_spine
)
SELECT sp.user_id, sp.label_ts, sp.churned,
       c.country,
       c.signup_rank_in_country,
       c.country_size,
       date_diff('{unit}', c.signup_ts, sp.label_ts) AS tenure_{unit}s
FROM spine sp
LEFT JOIN int_country_cohorts c ON c.user_id = sp.user_id
