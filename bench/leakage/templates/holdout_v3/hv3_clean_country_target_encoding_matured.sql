-- leak: clean
-- param m: 0 | 20
-- Smoothed churn rate of the user's country, from labels that are final at t only.
SELECT l.user_id, l.label_ts, l.churned, en.country,
       (SELECT (COALESCE(SUM(p.churned), 0) + {m} * 0.1) / NULLIF(COUNT(p.churned) + {m}, 0)
          FROM labels p
          JOIN entities pe ON pe.user_id = p.user_id
         WHERE pe.country = en.country
           AND p.label_ts + INTERVAL 14 DAY < l.label_ts) AS country_churn_rate_final
FROM labels l
LEFT JOIN entities en ON en.user_id = l.user_id
