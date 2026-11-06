-- leak: L7
-- age(x) with one argument is relative to today.
SELECT l.user_id, l.label_ts, l.churned,
       en.country,
       epoch(age(en.signup_ts)) / 86400.0 AS account_age_days
FROM marts.fct_training_spine l
LEFT JOIN staging.stg_entities en ON en.user_id = l.user_id
