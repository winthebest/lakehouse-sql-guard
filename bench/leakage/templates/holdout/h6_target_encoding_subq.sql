-- leak: L6
-- param agg: AVG | SUM
-- Leaky: the group churn feature is aggregated over the whole labels table, which includes this row's own label and later labels.
SELECT en.user_id, lab.label_ts, lab.churned,
       (SELECT {agg}(h.churned) FROM labels AS h JOIN entities AS he USING (user_id) WHERE he.country = en.country) AS country_churn
FROM labels AS lab
JOIN entities AS en ON en.user_id = lab.user_id
