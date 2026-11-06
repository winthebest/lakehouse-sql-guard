-- leak: L7
SELECT l.user_id, l.label_ts, l.churned,
       en.country,
       year(current_date) - year(en.signup_ts) AS customer_years
FROM marts.fct_training_spine l
LEFT JOIN staging.stg_entities en ON en.user_id = l.user_id
