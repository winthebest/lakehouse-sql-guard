-- leak: clean
-- param gap: 8 | 12
SELECT l.user_id, l.label_ts, l.churned,
       p.plan        AS plan_matured,
       p.monthly_fee AS fee_matured
FROM labels l
LEFT JOIN plan_history p
       ON p.user_id = l.user_id
      AND p.valid_from < l.label_ts - INTERVAL {gap} DAY
QUALIFY row_number() OVER (PARTITION BY l.user_id, l.label_ts
                           ORDER BY p.valid_from DESC NULLS LAST) = 1
