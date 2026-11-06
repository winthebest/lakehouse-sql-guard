-- leak: L7
SELECT l.user_id, l.label_ts, l.churned,
       COUNT(*) FILTER (WHERE e.event_ts >= current_date - INTERVAL 365 DAY) AS n_purchases_last_year,
       COUNT(e.event_id)                                                    AS n_known_orders
FROM labels l
LEFT JOIN events e
       ON e.user_id = l.user_id
      AND e.ingested_at < l.label_ts
GROUP BY ALL
