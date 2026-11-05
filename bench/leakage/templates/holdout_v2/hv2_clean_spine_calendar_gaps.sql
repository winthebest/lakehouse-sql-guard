-- leak: clean
-- param offset: 1 | 2
-- param unit: hour | day
-- Only earlier spine rows of the same user are read (LAG looks back), and their
-- prediction times exist at t. Calendar parts of t itself are known.
SELECT user_id, label_ts, churned,
       dayofweek(label_ts) AS prediction_dow,
       hour(label_ts)      AS prediction_hour,
       date_diff('{unit}', LAG(label_ts, {offset}) OVER w, label_ts) AS gap_since_prior_prediction,
       row_number() OVER w - 1 AS n_prior_predictions
FROM labels
WINDOW w AS (PARTITION BY user_id ORDER BY label_ts)
