-- leak: clean
-- Prior churn history of the same user, restricted to labels that are final at t.
SELECT l.user_id, l.label_ts, l.churned,
       COUNT(p.label_ts) AS n_final_prior_labels,
       SUM(p.churned)    AS n_final_prior_churns
FROM marts.fct_training_spine l
LEFT JOIN marts.fct_training_spine p
       ON p.user_id = l.user_id
      AND p.label_ts + INTERVAL 14 DAY < l.label_ts
GROUP BY l.user_id, l.label_ts, l.churned
