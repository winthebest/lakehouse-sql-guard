-- leak: clean
-- param gap: 8 | 10
-- The plan in effect at s = t - gap (gap >= 8 days). Every version that began before s was
-- loaded before t, so a valid_to < s is already visible at t and the lookup is safe.
SELECT l.user_id, l.label_ts, l.churned,
       p.plan        AS plan_lagged,
       p.monthly_fee AS fee_lagged
FROM labels l
LEFT JOIN plan_history p
       ON p.user_id = l.user_id
      AND p.valid_from < l.label_ts - INTERVAL {gap} DAY
      AND (p.valid_to IS NULL OR p.valid_to >= l.label_ts - INTERVAL {gap} DAY)
