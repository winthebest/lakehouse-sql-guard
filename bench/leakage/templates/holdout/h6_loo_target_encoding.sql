-- leak: L6
-- param grp: en.country | en.country, CAST(en.signup_ts AS DATE) < DATE '2025-07-01'
-- Leaky: the leave-one-out churn rate pools the labels of every other prediction in the group, including ones whose outcome had not been observed at this prediction time.
SELECT lab.user_id, lab.label_ts, lab.churned,
       (SUM(lab.churned) OVER (PARTITION BY {grp}) - lab.churned) * 1.0
         / NULLIF(COUNT(*) OVER (PARTITION BY {grp}) - 1, 0) AS loo_churn_rate
FROM labels AS lab
JOIN entities AS en ON en.user_id = lab.user_id
