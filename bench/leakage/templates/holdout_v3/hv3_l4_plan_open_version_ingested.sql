-- leak: L4
SELECT l.user_id, l.label_ts, l.churned, p.plan, p.monthly_fee
FROM labels l
LEFT JOIN plan_history p
       ON p.user_id = l.user_id
      AND p.ingested_at < l.label_ts
      AND p.valid_to IS NULL
