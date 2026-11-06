-- leak: clean
SELECT l.user_id, l.label_ts, l.churned,
       en.country,
       date_diff('day', en.signup_ts, l.label_ts) AS account_age_days,
       hour(en.signup_ts)                         AS signup_hour,
       dayofweek(en.signup_ts) IN (0, 6)          AS signed_up_on_weekend
FROM labels l
LEFT JOIN entities en ON en.user_id = l.user_id
