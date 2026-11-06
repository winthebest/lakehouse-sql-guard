-- leak: clean
SELECT l.user_id, l.label_ts, l.churned,
       list(p.plan ORDER BY p.valid_from DESC, p.ingested_at DESC)[1]        AS current_plan,
       list(p.plan ORDER BY p.valid_from DESC, p.ingested_at DESC)[2]        AS previous_plan,
       list(p.monthly_fee ORDER BY p.valid_from DESC, p.ingested_at DESC)[1]
         - list(p.monthly_fee ORDER BY p.valid_from DESC, p.ingested_at DESC)[2] AS last_fee_change
FROM labels l
LEFT JOIN plan_history p
       ON p.user_id = l.user_id
      AND p.ingested_at < l.label_ts
GROUP BY ALL
