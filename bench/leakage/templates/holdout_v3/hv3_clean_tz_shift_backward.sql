-- leak: clean
-- param zone: America/New_York | America/Los_Angeles
-- t read as UTC and shown in a zone west of UTC: the bound moves earlier, never later.
SELECT l.user_id, l.label_ts, l.churned,
       COUNT(e.event_id) AS n_orders_14d,
       SUM(e.amount)     AS spend_14d
FROM labels l
LEFT JOIN events e
       ON e.user_id = l.user_id
      AND e.ingested_at < timezone('{zone}', timezone('UTC', l.label_ts))
      AND e.event_ts >= l.label_ts - INTERVAL 14 DAY
GROUP BY ALL
