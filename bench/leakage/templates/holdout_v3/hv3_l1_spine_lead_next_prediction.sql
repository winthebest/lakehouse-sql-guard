-- leak: L1
SELECT user_id, label_ts, churned,
       date_diff('hour', label_ts, LEAD(label_ts) OVER w) AS hours_to_next_prediction,
       LEAD(label_ts) OVER w IS NULL                      AS is_last_prediction
FROM analytics.marts.fct_training_spine
WINDOW w AS (PARTITION BY user_id ORDER BY label_ts)
