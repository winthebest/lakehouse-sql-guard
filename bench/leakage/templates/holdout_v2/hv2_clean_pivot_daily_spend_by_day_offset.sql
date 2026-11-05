-- leak: clean
-- param measure: amount | CASE WHEN event_id IS NOT NULL THEN 1 END
-- Pivot-like conditional aggregation by calendar-day offset from t. The date cast is only
-- used to bucket purchases already loaded before t; it never widens the upper bound.
WITH known AS (
    SELECT l.user_id, l.label_ts, l.churned, e.amount, e.event_id,
           date_diff('day', CAST(e.event_ts AS DATE), CAST(l.label_ts AS DATE)) AS day_offset
    FROM labels l
    LEFT JOIN events e
      ON e.user_id = l.user_id
     AND e.ingested_at < l.label_ts
     AND e.event_ts >= CAST(l.label_ts AS DATE) - INTERVAL 7 DAY
)
SELECT user_id, label_ts, churned,
       COALESCE(SUM({measure}) FILTER (WHERE day_offset = 1), 0)            AS d1,
       COALESCE(SUM({measure}) FILTER (WHERE day_offset = 2), 0)            AS d2,
       COALESCE(SUM({measure}) FILTER (WHERE day_offset BETWEEN 3 AND 7), 0) AS d3_7
FROM known
GROUP BY user_id, label_ts, churned
