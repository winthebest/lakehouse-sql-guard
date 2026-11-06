-- leak: clean
-- A strict date comparison keeps only loads from before the prediction day.
SELECT l.user_id, l.label_ts, l.churned,
       COUNT(e.event_id) AS n_orders_30d,
       SUM(e.amount)     AS spend_30d
FROM labels l
LEFT JOIN staging.stg_events e
       ON e.user_id = l.user_id
      AND CAST(e.ingested_at AS DATE) < CAST(l.label_ts AS DATE)
      AND e.event_ts >= l.label_ts - INTERVAL 30 DAY
GROUP BY ALL
