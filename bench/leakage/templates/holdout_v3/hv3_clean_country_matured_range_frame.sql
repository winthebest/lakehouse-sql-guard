-- leak: clean
-- The frame stops 15 days before t: every label inside it is final at t.
WITH spine AS (
    SELECT l.user_id, l.label_ts, l.churned, en.country
    FROM labels l
    LEFT JOIN entities en ON en.user_id = l.user_id
)
SELECT user_id, label_ts, churned, country,
       AVG(churned)   OVER w AS country_churn_rate_final,
       COUNT(churned) OVER w AS n_final_labels
FROM spine
WINDOW w AS (PARTITION BY country ORDER BY label_ts
             RANGE BETWEEN UNBOUNDED PRECEDING AND INTERVAL 15 DAY PRECEDING)
