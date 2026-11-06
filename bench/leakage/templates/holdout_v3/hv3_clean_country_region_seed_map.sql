-- leak: clean
WITH region_map(country, region) AS (
    VALUES ('US', 'NA'), ('CA', 'NA'), ('GB', 'EU'), ('DE', 'EU'), ('FR', 'EU'),
           ('VN', 'APAC'), ('JP', 'APAC'), ('AU', 'APAC')
)
SELECT l.user_id, l.label_ts, l.churned,
       en.country,
       COALESCE(r.region, 'OTHER')                     AS region,
       date_diff('day', en.signup_ts, l.label_ts) < 30 AS is_new_account
FROM marts.fct_training_spine l
LEFT JOIN staging.stg_entities en ON en.user_id = l.user_id
LEFT JOIN region_map r            ON r.country = en.country
