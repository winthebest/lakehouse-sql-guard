-- leak: clean
-- param w: 14 | 30
WITH active_days AS (
    SELECT l.user_id, l.label_ts, CAST(e.event_ts AS DATE) AS d
    FROM labels l
    JOIN events e
      ON e.user_id = l.user_id
     AND e.ingested_at < l.label_ts
     AND e.event_ts >= l.label_ts - INTERVAL {w} DAY
    UNION
    SELECT l.user_id, l.label_ts, CAST(p.valid_from AS DATE) AS d
    FROM labels l
    JOIN plan_history p
      ON p.user_id = l.user_id
     AND p.ingested_at < l.label_ts
     AND p.valid_from >= l.label_ts - INTERVAL {w} DAY
)
SELECT l.user_id, l.label_ts, l.churned, COUNT(a.d) AS n_active_days
FROM labels l
LEFT JOIN active_days a ON a.user_id = l.user_id AND a.label_ts = l.label_ts
GROUP BY l.user_id, l.label_ts, l.churned
