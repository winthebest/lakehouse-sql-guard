-- leak: L6
SELECT l.user_id, l.label_ts, l.churned,
       COUNT(p.label_ts) AS n_prior_labels,
       SUM(p.churned)    AS n_prior_churns
FROM labels l
LEFT JOIN labels p
       ON p.user_id = l.user_id
      AND p.label_ts < l.label_ts
GROUP BY l.user_id, l.label_ts, l.churned
