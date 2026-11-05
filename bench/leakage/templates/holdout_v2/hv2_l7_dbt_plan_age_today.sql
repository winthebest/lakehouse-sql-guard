-- leak: L7
-- param unit: day | week
-- The plan is the newest version loaded before t, but its age is computed with today().
WITH spine AS (
    SELECT user_id, label_ts, churned
    FROM marts.fct_training_spine
),
stg_plans AS (
    SELECT user_id, plan, valid_from, ingested_at
    FROM snapshots.plan_history_snapshot
),
int_plan_pit AS (
    SELECT sp.user_id, sp.label_ts, p.plan, p.valid_from
    FROM spine sp
    JOIN stg_plans p ON p.user_id = sp.user_id AND p.ingested_at < sp.label_ts
    QUALIFY row_number() OVER (PARTITION BY sp.user_id, sp.label_ts ORDER BY p.valid_from DESC) = 1
)
SELECT sp.user_id, sp.label_ts, sp.churned,
       ip.plan,
       date_diff('{unit}', ip.valid_from, CAST(today() AS TIMESTAMP)) AS plan_age_{unit}s
FROM spine sp
LEFT JOIN int_plan_pit ip
  ON ip.user_id = sp.user_id AND ip.label_ts = sp.label_ts
