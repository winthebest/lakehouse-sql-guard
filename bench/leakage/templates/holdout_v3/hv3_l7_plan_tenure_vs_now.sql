-- leak: L7
SELECT l.user_id, l.label_ts, l.churned, p.plan,
       date_diff('day', p.valid_from, current_timestamp) AS days_on_plan
FROM labels l
LEFT JOIN plan_history p
       ON p.user_id = l.user_id
      AND p.ingested_at < l.label_ts
QUALIFY row_number() OVER (PARTITION BY l.user_id, l.label_ts
                           ORDER BY p.valid_from DESC NULLS LAST) = 1
