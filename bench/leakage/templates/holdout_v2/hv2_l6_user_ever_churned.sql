-- leak: L6
-- param fn: MAX | SUM
-- A window over the user's whole spine partition of churned: it includes the row's own label.
SELECT user_id, label_ts, churned,
       {fn}(churned) OVER (PARTITION BY user_id) AS user_churn_history
FROM labels
