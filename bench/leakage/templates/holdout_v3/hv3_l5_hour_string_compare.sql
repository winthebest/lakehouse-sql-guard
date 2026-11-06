-- leak: L5
SELECT l.user_id, l.label_ts, l.churned,
       COUNT(e.event_id) AS n_orders_14d,
       SUM(e.amount)     AS spend_14d
FROM labels l
LEFT JOIN events e
       ON e.user_id = l.user_id
      AND strftime(e.ingested_at, '%Y-%m-%d %H') <= strftime(l.label_ts, '%Y-%m-%d %H')
      AND e.event_ts >= l.label_ts - INTERVAL 14 DAY
GROUP BY ALL
