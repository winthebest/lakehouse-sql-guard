-- leak: L4
-- param asof: (p.valid_to IS NULL OR p.valid_to >= l.label_ts) | COALESCE(p.valid_to, TIMESTAMP '9999-12-31') > l.label_ts
SELECT l.user_id, l.label_ts, l.churned, p.plan, p.monthly_fee
FROM labels l
LEFT JOIN plan_history p
       ON p.user_id = l.user_id
      AND p.ingested_at < l.label_ts
      AND p.valid_from  < l.label_ts
      AND {asof}
