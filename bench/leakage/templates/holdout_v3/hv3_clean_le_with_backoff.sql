-- leak: clean
-- param h: 1 | 6
-- A <= bound is fine here: it is set h hours before t, so nothing stamped t is included.
SELECT l.user_id, l.label_ts, l.churned,
       COUNT(e.event_id) AS n_orders_14d,
       SUM(e.amount)     AS spend_14d
FROM labels l
LEFT JOIN events e
       ON e.user_id = l.user_id
      AND e.ingested_at <= l.label_ts - INTERVAL {h} HOUR
      AND e.event_ts >= l.label_ts - INTERVAL 14 DAY
GROUP BY ALL
