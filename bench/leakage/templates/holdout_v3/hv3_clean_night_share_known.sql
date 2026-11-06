-- leak: clean
-- param w: 30 | 60
SELECT l.user_id, l.label_ts, l.churned,
       COUNT(*) FILTER (WHERE hour(e.event_ts) < 6) / NULLIF(COUNT(e.event_id), 0)::DOUBLE AS night_share,
       COUNT(DISTINCT dayofweek(e.event_ts))                                           AS n_distinct_weekdays
FROM labels l
LEFT JOIN events e
       ON e.user_id = l.user_id
      AND e.ingested_at < l.label_ts
      AND e.event_ts >= l.label_ts - INTERVAL {w} DAY
GROUP BY ALL
