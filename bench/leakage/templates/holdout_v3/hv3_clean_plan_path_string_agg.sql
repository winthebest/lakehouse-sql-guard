-- leak: clean
SELECT l.user_id, l.label_ts, l.churned,
       string_agg(p.plan, '>' ORDER BY p.valid_from, p.ingested_at) AS plan_path_known,
       COUNT(DISTINCT p.plan)                                      AS n_distinct_plans_known
FROM labels l
LEFT JOIN plan_history p
       ON p.user_id = l.user_id
      AND p.ingested_at < l.label_ts
GROUP BY l.user_id, l.label_ts, l.churned
