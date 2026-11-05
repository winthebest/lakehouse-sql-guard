-- leak: L2
-- param col: amount | event_ts
-- first_value over the user's purchases ordered newest first: the newest purchase today.
WITH ranked AS (
    SELECT user_id,
           first_value({col}) OVER (PARTITION BY user_id ORDER BY event_ts DESC, event_id DESC) AS newest_val
    FROM events
)
SELECT l.user_id, l.label_ts, l.churned,
       nv.newest_val AS newest_purchase_{col}
FROM labels l
LEFT JOIN (SELECT DISTINCT user_id, newest_val FROM ranked) nv
  ON nv.user_id = l.user_id
