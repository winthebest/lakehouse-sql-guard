-- leak: L5
-- param zone: Asia/Saigon | Asia/Tokyo | Europe/Berlin
-- t is read as UTC and turned into local business time before comparing with naive UTC loads.
SELECT l.user_id, l.label_ts, l.churned,
       COUNT(e.event_id) AS n_orders_14d,
       SUM(e.amount)     AS spend_14d
FROM marts.fct_training_spine l
LEFT JOIN staging.stg_events e
       ON e.user_id = l.user_id
      AND e.ingested_at < timezone('{zone}', timezone('UTC', l.label_ts))
      AND e.event_ts >= l.label_ts - INTERVAL 14 DAY
GROUP BY ALL
