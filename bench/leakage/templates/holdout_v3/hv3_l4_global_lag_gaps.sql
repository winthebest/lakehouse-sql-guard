-- leak: L4
WITH gaps AS (
    SELECT user_id, event_ts, ingested_at,
           date_diff('hour', LAG(event_ts) OVER (PARTITION BY user_id ORDER BY event_ts, event_id),
                     event_ts) AS gap_h
    FROM events
)
SELECT l.user_id, l.label_ts, l.churned,
       AVG(g.gap_h) AS mean_gap_h,
       MAX(g.gap_h) AS max_gap_h
FROM labels l
LEFT JOIN gaps g
       ON g.user_id = l.user_id
      AND g.ingested_at < l.label_ts
      AND g.event_ts >= l.label_ts - INTERVAL 30 DAY
GROUP BY ALL
