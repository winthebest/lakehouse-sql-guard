-- leak: L6
WITH spine AS (
    SELECT l.user_id, l.label_ts, l.churned, en.country
    FROM labels l
    LEFT JOIN entities en ON en.user_id = l.user_id
)
SELECT user_id, label_ts, churned, country,
       AVG(churned) OVER (PARTITION BY country) AS country_churn_rate
FROM spine
