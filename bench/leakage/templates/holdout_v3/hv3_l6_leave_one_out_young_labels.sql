-- leak: L6
WITH spine AS (
    SELECT l.user_id, l.label_ts, l.churned, en.country
    FROM marts.fct_training_spine l
    LEFT JOIN staging.stg_entities en ON en.user_id = l.user_id
)
SELECT user_id, label_ts, churned, country,
       (SUM(churned) OVER p - churned) / NULLIF(COUNT(*) OVER p - 1, 0) AS country_churn_rate_loo
FROM spine
WINDOW p AS (PARTITION BY country)
