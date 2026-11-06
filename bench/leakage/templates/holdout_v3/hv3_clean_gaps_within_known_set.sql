-- leak: clean
WITH known AS (
    SELECT l.user_id, l.label_ts, l.churned, e.event_ts
    FROM labels l
    LEFT JOIN staging.stg_events e
           ON e.user_id = l.user_id
          AND e.ingested_at < l.label_ts
),
gaps AS (
    SELECT *,
           date_diff('hour', LAG(event_ts) OVER (PARTITION BY user_id, label_ts ORDER BY event_ts),
                     event_ts) AS gap_h
    FROM known
)
SELECT user_id, label_ts, churned,
       AVG(gap_h)    AS mean_gap_h,
       MAX(gap_h)    AS max_gap_h,
       median(gap_h) AS median_gap_h
FROM gaps
GROUP BY ALL
