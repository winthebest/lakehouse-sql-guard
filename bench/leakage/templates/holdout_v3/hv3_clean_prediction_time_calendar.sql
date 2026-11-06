-- leak: clean
SELECT l.user_id, l.label_ts, l.churned,
       hour(l.label_ts)                            AS prediction_hour,
       dayofweek(l.label_ts)                       AS prediction_dow,
       l.label_ts = date_trunc('day', l.label_ts)  AS prediction_at_midnight,
       day(l.label_ts)                             AS day_of_month,
       date_diff('week', en.signup_ts, l.label_ts) AS account_age_weeks
FROM labels l
LEFT JOIN analytics.staging.stg_entities en ON en.user_id = l.user_id
