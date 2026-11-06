-- leak: clean
SELECT l.user_id, l.label_ts, l.churned,
       COUNT(*) FILTER (WHERE e.ingested_at >= l.label_ts - INTERVAL 1 DAY) AS n_loaded_last_24h,
       COUNT(*) FILTER (WHERE e.ingested_at >= l.label_ts - INTERVAL 2 DAY
                          AND e.ingested_at <  l.label_ts - INTERVAL 1 DAY) AS n_loaded_24_48h,
       COUNT(*) FILTER (WHERE e.ingested_at <  l.label_ts - INTERVAL 2 DAY) AS n_loaded_2_7d
FROM labels l
LEFT JOIN events e
       ON e.user_id = l.user_id
      AND e.ingested_at <  l.label_ts
      AND e.ingested_at >= l.label_ts - INTERVAL 7 DAY
GROUP BY ALL
