-- leak: L3
SELECT l.user_id, l.label_ts, l.churned,
       SUM(e.amount)                                   AS spend_30d_known,
       SUM(e.amount) / (SELECT SUM(amount) FROM events) AS share_of_all_revenue
FROM labels l
LEFT JOIN events e
       ON e.user_id = l.user_id
      AND e.ingested_at < l.label_ts
      AND e.event_ts >= l.label_ts - INTERVAL 30 DAY
GROUP BY ALL
