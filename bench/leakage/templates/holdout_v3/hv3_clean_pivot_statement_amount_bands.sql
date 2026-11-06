-- leak: clean
-- param w: 30 | 90
WITH known AS (
    SELECT l.user_id, l.label_ts, l.churned, e.amount,
           CASE WHEN e.amount < 15 THEN 'low'
                WHEN e.amount < 60 THEN 'mid'
                WHEN e.amount IS NOT NULL THEN 'high' END AS band
    FROM labels l
    LEFT JOIN staging.stg_events e
           ON e.user_id = l.user_id
          AND e.ingested_at < l.label_ts
          AND e.event_ts >= l.label_ts - INTERVAL {w} DAY
),
pivoted AS (
    PIVOT known
    ON band IN ('low', 'mid', 'high')
    USING SUM(amount) AS spend, COUNT(amount) AS n
    GROUP BY user_id, label_ts, churned
)
SELECT * FROM pivoted
