-- leak: clean
WITH windows(w_days) AS (VALUES (7), (30), (90)),
known AS (
    SELECT l.user_id, l.label_ts, l.churned, w.w_days, e.amount
    FROM labels l
    CROSS JOIN windows w
    LEFT JOIN events e
           ON e.user_id = l.user_id
          AND e.ingested_at < l.label_ts
          AND e.event_ts >= l.label_ts - w.w_days * INTERVAL 1 DAY
)
SELECT user_id, label_ts, churned,
       SUM(amount) FILTER (WHERE w_days = 7)  AS spend_7d,
       SUM(amount) FILTER (WHERE w_days = 30) AS spend_30d,
       SUM(amount) FILTER (WHERE w_days = 90) AS spend_90d
FROM known
GROUP BY ALL
