-- leak: L4
-- param d: 1 | 4 | 7
SELECT l.user_id, l.label_ts, l.churned, p.plan AS plan_lagged, p.monthly_fee AS fee_lagged
FROM labels l
LEFT JOIN analytics.snapshots.plan_history_snapshot p
       ON p.user_id = l.user_id
      AND p.valid_from < l.label_ts - INTERVAL {d} DAY
      AND (p.valid_to IS NULL OR p.valid_to >= l.label_ts - INTERVAL {d} DAY)
