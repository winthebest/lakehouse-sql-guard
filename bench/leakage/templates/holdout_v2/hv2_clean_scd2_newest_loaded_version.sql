-- leak: clean
-- param rel: analytics.snapshots.plan_history_snapshot | snapshots.plan_history_snapshot | plan_history
-- param col: plan | monthly_fee
-- Point-in-time SCD2 lookup the safe way: among versions loaded before t (ingested_at < t),
-- take the one with the newest valid_from. valid_to is never read.
WITH spine AS (
    SELECT user_id, label_ts, churned
    FROM analytics.marts.fct_training_spine
),
plan_versions AS (
    SELECT user_id, plan, monthly_fee, valid_from, ingested_at
    FROM {rel}
),
plan_known AS (
    SELECT sp.user_id, sp.label_ts, pv.{col} AS plan_value, pv.valid_from
    FROM spine sp
    JOIN plan_versions pv
      ON pv.user_id = sp.user_id
     AND pv.ingested_at < sp.label_ts
    QUALIFY row_number() OVER (PARTITION BY sp.user_id, sp.label_ts
                               ORDER BY pv.valid_from DESC) = 1
)
SELECT sp.user_id, sp.label_ts, sp.churned,
       pk.plan_value AS known_{col},
       date_diff('day', pk.valid_from, sp.label_ts) AS days_since_known_plan_start
FROM spine sp
LEFT JOIN plan_known pk
  ON pk.user_id = sp.user_id AND pk.label_ts = sp.label_ts
