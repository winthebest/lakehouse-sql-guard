-- leak: L3
WITH country_benchmark AS (
    SELECT x.country, SUM(e.amount) / COUNT(DISTINCT e.user_id) AS country_spend_per_buyer
    FROM events e
    JOIN entities x ON x.user_id = e.user_id
    GROUP BY x.country
),
user_spend AS (
    SELECT l.user_id, l.label_ts, l.churned, en.country, COALESCE(SUM(e.amount), 0) AS spend_30d
    FROM labels l
    LEFT JOIN entities en ON en.user_id = l.user_id
    LEFT JOIN events e
           ON e.user_id = l.user_id
          AND e.ingested_at < l.label_ts
          AND e.event_ts >= l.label_ts - INTERVAL 30 DAY
    GROUP BY l.user_id, l.label_ts, l.churned, en.country
)
SELECT u.user_id, u.label_ts, u.churned, u.spend_30d,
       u.spend_30d / NULLIF(cb.country_spend_per_buyer, 0) AS spend_vs_country
FROM user_spend u
LEFT JOIN country_benchmark cb ON cb.country = u.country
