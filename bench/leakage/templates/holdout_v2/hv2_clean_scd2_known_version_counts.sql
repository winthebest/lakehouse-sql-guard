-- leak: clean
-- param rel: plan_history | analytics.snapshots.plan_history_snapshot
-- param window: 30 | 365
-- Counts only plan versions loaded before t (ingested_at < t); valid_to is not used.
SELECT l.user_id, l.label_ts, l.churned,
       COUNT(p.user_id)      AS n_known_versions,
       COUNT(DISTINCT p.plan) AS n_known_distinct_plans,
       COUNT(p.user_id) FILTER (WHERE p.valid_from >= l.label_ts - INTERVAL {window} DAY)
                             AS n_known_changes_{window}d
FROM labels l
LEFT JOIN {rel} p
  ON p.user_id = l.user_id
 AND p.ingested_at < l.label_ts
GROUP BY l.user_id, l.label_ts, l.churned
