-- leak: L2
WITH ev AS (
    SELECT DISTINCT user_id,
           first_value(amount)   OVER (PARTITION BY user_id ORDER BY event_ts DESC, event_id DESC) AS most_recent_amount,
           first_value(event_ts) OVER (PARTITION BY user_id ORDER BY event_ts DESC, event_id DESC) AS most_recent_ts
    FROM events
)
SELECT l.user_id, l.label_ts, l.churned,
       ev.most_recent_amount,
       date_diff('day', ev.most_recent_ts, l.label_ts) AS days_since_most_recent
FROM labels l
LEFT JOIN ev ON ev.user_id = l.user_id
