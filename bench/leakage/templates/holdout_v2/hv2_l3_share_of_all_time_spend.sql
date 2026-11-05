-- leak: L3
-- param days: 30 | 90
-- The numerator is point-in-time (ingested_at < t), but the normaliser is the total spend
-- of all users over the whole table.
WITH spine AS (
    SELECT user_id, label_ts, churned
    FROM labels
),
recent_spend AS (
    SELECT sp.user_id, sp.label_ts, SUM(e.amount) AS spend
    FROM spine sp
    JOIN events e
      ON e.user_id = sp.user_id
     AND e.ingested_at < sp.label_ts
     AND e.event_ts >= sp.label_ts - INTERVAL {days} DAY
    GROUP BY sp.user_id, sp.label_ts
),
all_time AS (
    SELECT SUM(amount) AS total_spend_all_users
    FROM events
)
SELECT sp.user_id, sp.label_ts, sp.churned,
       COALESCE(r.spend, 0) / a.total_spend_all_users AS share_of_all_time_spend
FROM spine sp
CROSS JOIN all_time a
LEFT JOIN recent_spend r
  ON r.user_id = sp.user_id AND r.label_ts = sp.label_ts
