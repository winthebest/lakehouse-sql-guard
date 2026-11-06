-- leak: L3
WITH user_totals AS (
    SELECT user_id, SUM(amount) AS total FROM events GROUP BY user_id
),
population AS (
    SELECT AVG(total) AS mu, stddev_pop(total) AS sigma FROM user_totals
),
known AS (
    SELECT l.user_id, l.label_ts, l.churned, COALESCE(SUM(e.amount), 0) AS known_spend
    FROM labels l
    LEFT JOIN events e
           ON e.user_id = l.user_id
          AND e.ingested_at < l.label_ts
    GROUP BY l.user_id, l.label_ts, l.churned
)
SELECT k.user_id, k.label_ts, k.churned,
       k.known_spend,
       (k.known_spend - p.mu) / NULLIF(p.sigma, 0) AS spend_z
FROM known k
CROSS JOIN population p
