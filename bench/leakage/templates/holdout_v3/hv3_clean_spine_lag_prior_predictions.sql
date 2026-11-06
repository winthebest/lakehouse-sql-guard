-- leak: clean
SELECT user_id, label_ts, churned,
       date_diff('hour', LAG(label_ts) OVER w, label_ts) AS hours_since_prev_prediction,
       row_number() OVER w - 1                           AS n_prior_predictions
FROM analytics.marts.fct_training_spine
WINDOW w AS (PARTITION BY user_id ORDER BY label_ts)
