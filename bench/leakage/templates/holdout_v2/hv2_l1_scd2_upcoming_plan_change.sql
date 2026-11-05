-- leak: L1
-- param days: 7 | 14 | 30
-- param rel: analytics.snapshots.plan_history_snapshot | plan_history
-- A lower bound at t: versions with valid_from >= t took effect after t (and were loaded
-- even later), so the "next plan" is read from the future.
WITH spine AS (
    SELECT user_id, label_ts, churned
    FROM analytics.marts.fct_training_spine
),
plan_versions AS (
    SELECT user_id, plan, monthly_fee, valid_from
    FROM {rel}
),
upcoming AS (
    SELECT sp.user_id, sp.label_ts,
           MIN(pv.valid_from)              AS next_change_at,
           arg_min(pv.plan, pv.valid_from) AS next_plan
    FROM spine sp
    JOIN plan_versions pv
      ON pv.user_id = sp.user_id
     AND pv.valid_from >= sp.label_ts
     AND pv.valid_from <  sp.label_ts + INTERVAL {days} DAY
    GROUP BY sp.user_id, sp.label_ts
)
SELECT sp.user_id, sp.label_ts, sp.churned,
       u.next_plan,
       u.next_change_at IS NOT NULL AS plan_change_within_{days}d
FROM spine sp
LEFT JOIN upcoming u
  ON u.user_id = sp.user_id AND u.label_ts = sp.label_ts
