-- leak: clean
-- param bound: e.ingested_at < l.label_ts | e.event_ts < l.label_ts - INTERVAL 8 DAY
SELECT l.user_id, l.label_ts, l.churned,
       COUNT(*) FILTER (WHERE e.amount < 10)                    AS n_small,
       COUNT(*) FILTER (WHERE e.amount >= 10 AND e.amount < 50) AS n_medium,
       COUNT(*) FILTER (WHERE e.amount >= 50)                   AS n_large,
       SUM(e.amount) FILTER (WHERE dayofweek(e.event_ts) IN (0, 6)) AS weekend_spend
FROM labels l
LEFT JOIN events e
       ON e.user_id = l.user_id
      AND {bound}
      AND e.event_ts >= l.label_ts - INTERVAL 60 DAY
GROUP BY ALL
