-- leak: L6
-- param power: 1.0 | 0.5
-- A class-balance weight emitted as a column: its value is chosen by the row's own churned
-- (and scaled by label counts over the whole spine).
SELECT l.user_id, l.label_ts, l.churned,
       CASE WHEN l.churned = 1
            THEN power(w.n_neg * 1.0 / NULLIF(w.n_pos, 0), {power})
            ELSE 1.0 END AS class_weight
FROM labels l
CROSS JOIN (
    SELECT COUNT(*) FILTER (WHERE churned = 1) AS n_pos,
           COUNT(*) FILTER (WHERE churned = 0) AS n_neg
    FROM labels
) w
