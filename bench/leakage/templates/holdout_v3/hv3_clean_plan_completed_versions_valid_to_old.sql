-- leak: clean
-- param gap: 8 | 10
-- valid_to is safe here: a version that ended more than 8 days before t was closed by a load
-- that happened before t.
SELECT l.user_id, l.label_ts, l.churned,
       COUNT(p.user_id)                                  AS n_completed_plans,
       AVG(date_diff('day', p.valid_from, p.valid_to))   AS avg_completed_plan_days
FROM labels l
LEFT JOIN plan_history p
       ON p.user_id = l.user_id
      AND p.valid_to < l.label_ts - INTERVAL {gap} DAY
GROUP BY ALL
