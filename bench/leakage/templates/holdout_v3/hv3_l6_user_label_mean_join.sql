-- leak: L6
WITH user_propensity AS (
    SELECT user_id, AVG(churned) AS user_churn_rate
    FROM labels
    GROUP BY user_id
)
SELECT l.user_id, l.label_ts, l.churned,
       up.user_churn_rate,
       (SELECT COALESCE(SUM(e.amount), 0) FROM events e
         WHERE e.user_id = l.user_id
           AND e.ingested_at < l.label_ts
           AND e.event_ts >= l.label_ts - INTERVAL 30 DAY) AS spend_30d
FROM labels l
LEFT JOIN user_propensity up ON up.user_id = l.user_id
