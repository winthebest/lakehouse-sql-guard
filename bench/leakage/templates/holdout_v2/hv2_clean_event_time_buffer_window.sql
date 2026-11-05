-- leak: clean
-- param buffer: 8 | 10 | 14
-- param span: 30 | 60
-- No ingestion bound, but every counted purchase has event_ts < t - 8 days or earlier,
-- and a purchase is always loaded within 8 days of event_ts, so it was loaded before t.
WITH lagged AS (
    SELECT l.user_id, l.label_ts, l.churned,
           COUNT(e.event_id) AS n_purchases_lagged,
           SUM(e.amount)     AS spend_lagged
    FROM labels l
    LEFT JOIN events e
      ON e.user_id = l.user_id
     AND e.event_ts <  l.label_ts - INTERVAL {buffer} DAY
     AND e.event_ts >= l.label_ts - INTERVAL {buffer} DAY - INTERVAL {span} DAY
    GROUP BY l.user_id, l.label_ts, l.churned
)
SELECT user_id, label_ts, churned,
       n_purchases_lagged,
       COALESCE(spend_lagged, 0) AS spend_lagged
FROM lagged
