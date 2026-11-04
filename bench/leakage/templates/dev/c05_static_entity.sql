-- leak: clean
-- param join: JOIN | LEFT JOIN
SELECT l.user_id, l.label_ts, l.churned, u.country,
       date_diff('day', u.signup_ts, l.label_ts) AS tenure_days
FROM labels l
{join} entities u ON u.user_id = l.user_id
