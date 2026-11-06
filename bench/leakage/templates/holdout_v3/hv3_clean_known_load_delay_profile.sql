-- leak: clean
SELECT l.user_id, l.label_ts, l.churned,
       AVG(date_diff('hour', e.event_ts, e.ingested_at))                          AS mean_load_delay_h,
       AVG(CASE WHEN e.ingested_at > e.event_ts + INTERVAL 1 DAY THEN 1.0 ELSE 0.0 END) AS share_late_loads
FROM labels l
LEFT JOIN events e
       ON e.user_id = l.user_id
      AND e.ingested_at < l.label_ts
      AND e.event_ts >= l.label_ts - INTERVAL 30 DAY
GROUP BY ALL
