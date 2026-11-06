-- leak: L2
WITH last_purchase AS (
    SELECT user_id, max_by(amount, event_ts) AS last_amount
    FROM events
    GROUP BY user_id
)
SELECT l.user_id, l.label_ts, l.churned, lp.last_amount
FROM labels l
LEFT JOIN last_purchase lp ON lp.user_id = l.user_id
