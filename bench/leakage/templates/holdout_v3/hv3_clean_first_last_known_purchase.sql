-- leak: clean
SELECT l.user_id, l.label_ts, l.churned,
       date_diff('day', MIN(e.event_ts), l.label_ts) AS days_since_first_known_purchase,
       date_diff('day', MAX(e.event_ts), l.label_ts) AS days_since_last_known_purchase,
       COUNT(DISTINCT CAST(e.event_ts AS DATE))     AS n_known_purchase_days
FROM labels l
LEFT JOIN events e
       ON e.user_id = l.user_id
      AND e.ingested_at < l.label_ts
GROUP BY l.user_id, l.label_ts, l.churned
