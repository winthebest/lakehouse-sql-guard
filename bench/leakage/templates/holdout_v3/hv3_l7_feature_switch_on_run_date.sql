-- leak: L7
-- param age: 365 | 730
WITH known_stats AS (
    SELECT l.user_id, l.label_ts, s.total_spend
    FROM labels l
    ASOF LEFT JOIN user_stats s
      ON s.user_id = l.user_id
     AND l.label_ts > s.updated_at
),
known_events AS (
    SELECT l.user_id, l.label_ts, COALESCE(SUM(e.amount), 0) AS spend_30d
    FROM labels l
    LEFT JOIN events e
           ON e.user_id = l.user_id
          AND e.ingested_at < l.label_ts
          AND e.event_ts >= l.label_ts - INTERVAL 30 DAY
    GROUP BY l.user_id, l.label_ts
)
SELECT l.user_id, l.label_ts, l.churned,
       CASE WHEN date_diff('day', CAST(l.label_ts AS DATE), current_date) > {age}
            THEN ks.total_spend          -- old rows: use the lifetime stats
            ELSE ke.spend_30d END AS spend_signal
FROM labels l
LEFT JOIN known_stats  ks ON ks.user_id = l.user_id AND ks.label_ts = l.label_ts
LEFT JOIN known_events ke ON ke.user_id = l.user_id AND ke.label_ts = l.label_ts
