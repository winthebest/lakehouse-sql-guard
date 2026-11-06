-- leak: L7
SELECT l.user_id, l.label_ts, l.churned,
       s.total_spend,
       date_diff('day', CAST(l.label_ts AS DATE), current_date)                  AS sample_age_days,
       1.0 / (1 + date_diff('day', CAST(l.label_ts AS DATE), current_date) / 30.0) AS sample_weight
FROM analytics.marts.fct_training_spine l
ASOF LEFT JOIN analytics.staging.stg_user_stats s
  ON s.user_id = l.user_id
 AND l.label_ts > s.updated_at
