-- leak: L3
-- param measure: COUNT(*) | SUM(amount)
-- param fn: percent_rank() | ntile(10)
-- Ranks users by an all-history aggregate of their purchases.
WITH lifetime AS (
    SELECT user_id, {measure} AS m
    FROM events
    GROUP BY user_id
),
ranked AS (
    SELECT user_id, {fn} OVER (ORDER BY m) AS activity_rank
    FROM lifetime
)
SELECT l.user_id, l.label_ts, l.churned,
       r.activity_rank
FROM labels l
LEFT JOIN ranked r ON r.user_id = l.user_id
