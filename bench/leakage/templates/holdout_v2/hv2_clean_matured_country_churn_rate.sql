-- leak: clean
-- param maturity: 14 | 21
-- param smoothing: 0 | 5
-- Target encoding over peer spine rows of the same country whose label is final:
-- b.label_ts + maturity days < a.label_ts with maturity >= 14 (strict).
WITH spine_geo AS (
    SELECT l.user_id, l.label_ts, l.churned, en.country
    FROM labels l
    JOIN entities en ON en.user_id = l.user_id
)
SELECT a.user_id, a.label_ts, a.churned,
       COUNT(b.churned) AS n_final_peer_labels,
       (COALESCE(SUM(b.churned), 0) + {smoothing} * 0.1)
         / NULLIF(COUNT(b.churned) + {smoothing}, 0) AS country_churn_rate_final
FROM spine_geo a
LEFT JOIN spine_geo b
  ON b.country = a.country
 AND b.label_ts + INTERVAL {maturity} DAY < a.label_ts
GROUP BY a.user_id, a.label_ts, a.churned
