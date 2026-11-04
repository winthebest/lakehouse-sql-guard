-- leak: L6
-- param scale: 1 | 24 | 1440
-- param join: JOIN | LEFT JOIN
WITH o AS (SELECT user_id, label_ts, days_to_churn * {scale} AS horizon FROM user_outcomes)
SELECT l.user_id, l.label_ts, l.churned, o.horizon
FROM labels l
{join} o ON o.user_id = l.user_id AND o.label_ts = l.label_ts
