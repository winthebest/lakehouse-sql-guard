-- leak: clean
-- param buffer: 8 | 9 | 14
-- param join: LEFT JOIN | JOIN
-- The plan in effect at t - buffer (buffer >= 8 days). Every version with valid_from before
-- that instant was loaded before t, and if its successor began before that instant, the
-- successor (and so the closing valid_to) was loaded before t too.
WITH spine AS (
    SELECT user_id, label_ts, churned
    FROM marts.fct_training_spine
),
snap AS (
    SELECT user_id, plan, monthly_fee, valid_from, valid_to
    FROM snapshots.plan_history_snapshot
),
plan_lagged AS (
    SELECT sp.user_id, sp.label_ts, s.plan, s.monthly_fee
    FROM spine sp
    JOIN snap s
      ON s.user_id = sp.user_id
     AND s.valid_from < sp.label_ts - INTERVAL {buffer} DAY
     AND (s.valid_to IS NULL OR s.valid_to >= sp.label_ts - INTERVAL {buffer} DAY)
)
SELECT sp.user_id, sp.label_ts, sp.churned,
       p.plan        AS plan_{buffer}d_ago,
       p.monthly_fee AS fee_{buffer}d_ago
FROM spine sp
{join} plan_lagged p
  ON p.user_id = sp.user_id AND p.label_ts = sp.label_ts
