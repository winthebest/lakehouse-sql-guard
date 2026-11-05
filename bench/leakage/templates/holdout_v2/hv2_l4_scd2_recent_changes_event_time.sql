-- leak: L4
-- param days: 14 | 30 | 60
-- param rel: plan_history | analytics.snapshots.plan_history_snapshot
-- Counts plan versions by valid_from < t only; a change that took effect before t but was
-- loaded at or after t is counted although it was unknown at t.
SELECT l.user_id, l.label_ts, l.churned,
       COUNT(p.user_id) FILTER (WHERE p.valid_from >= l.label_ts - INTERVAL {days} DAY)
           AS n_plan_changes_{days}d,
       COUNT(p.user_id) FILTER (WHERE p.plan = 'free'
                                  AND p.valid_from >= l.label_ts - INTERVAL {days} DAY)
           AS n_moves_to_free_{days}d
FROM labels l
LEFT JOIN {rel} p
  ON p.user_id = l.user_id
 AND p.valid_from < l.label_ts
GROUP BY l.user_id, l.label_ts, l.churned
