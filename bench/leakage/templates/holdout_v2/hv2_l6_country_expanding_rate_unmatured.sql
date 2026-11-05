-- leak: L6
-- param min_rows: 1 | 10
-- Expanding country churn rate over strictly earlier spine rows (ROWS ... 1 PRECEDING), so
-- the row's own label is left out, but almost all earlier labels are younger than 14 days
-- and not final at t.
WITH spine_geo AS (
    SELECT l.user_id, l.label_ts, l.churned, en.country
    FROM labels l
    JOIN entities en ON en.user_id = l.user_id
),
enc AS (
    SELECT user_id, label_ts, churned,
           AVG(churned) OVER w  AS rate_prior,
           COUNT(churned) OVER w AS n_prior
    FROM spine_geo
    WINDOW w AS (PARTITION BY country ORDER BY label_ts, user_id
                 ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING)
)
SELECT user_id, label_ts, churned,
       CASE WHEN n_prior >= {min_rows} THEN rate_prior END AS country_prior_churn_rate
FROM enc
