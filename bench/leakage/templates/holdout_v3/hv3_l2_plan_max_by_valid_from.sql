-- leak: L2
WITH plan_latest AS (
    SELECT user_id,
           max_by(plan, valid_from)        AS plan,
           max_by(monthly_fee, valid_from) AS monthly_fee
    FROM plan_history
    GROUP BY user_id
)
SELECT l.user_id, l.label_ts, l.churned, pl.plan, pl.monthly_fee, en.country
FROM labels l
LEFT JOIN plan_latest pl ON pl.user_id = l.user_id
LEFT JOIN entities en    ON en.user_id = l.user_id
