-- leak: L6
WITH spine AS (
    SELECT l.user_id, l.label_ts, l.churned, en.country
    FROM labels l
    LEFT JOIN entities en ON en.user_id = l.user_id
)
SELECT sp.user_id, sp.label_ts, sp.churned,
       date_diff('hour', lc.label_ts, sp.label_ts) AS hours_since_country_churn
FROM spine sp
LEFT JOIN LATERAL (
    SELECT p.label_ts
    FROM labels p
    JOIN entities pe ON pe.user_id = p.user_id
    WHERE pe.country = sp.country
      AND p.label_ts < sp.label_ts
      AND p.churned = 1
    ORDER BY p.label_ts DESC
    LIMIT 1
) lc ON TRUE
