-- leak: L6
-- param agg: AVG | SUM
-- param part: PARTITION BY u.country | PARTITION BY date_trunc('month', u.signup_ts) |
SELECT l.user_id, l.label_ts, l.churned, {agg}(l.churned) OVER ({part}) AS group_churn
FROM labels l
JOIN entities u ON u.user_id = l.user_id
