-- leak: L7
SELECT l.user_id, l.label_ts, l.churned,
       date_diff('day', CAST(MAX(e.event_ts) AS DATE), current_date) AS days_since_last_purchase,
       COUNT(e.event_id)                                            AS n_known_orders
FROM labels l
LEFT JOIN events e
       ON e.user_id = l.user_id
      AND e.ingested_at < l.label_ts
GROUP BY ALL
