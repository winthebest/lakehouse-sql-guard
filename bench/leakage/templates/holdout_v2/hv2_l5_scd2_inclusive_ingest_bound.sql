-- leak: L5
-- param col: plan | monthly_fee
-- param rel: snapshots.plan_history_snapshot | plan_history
-- The safe SCD2 shape (newest loaded version, no valid_to) but with ingested_at <= t: a
-- version loaded by the load at exactly t is taken.
WITH spine AS (
    SELECT user_id, label_ts, churned
    FROM marts.fct_training_spine
),
stg_plans AS (
    SELECT user_id, plan, monthly_fee, valid_from, ingested_at AS loaded_at
    FROM {rel}
),
int_plan_known AS (
    SELECT sp.user_id, sp.label_ts, p.{col} AS v
    FROM spine sp
    JOIN stg_plans p
      ON p.user_id = sp.user_id
     AND p.loaded_at <= sp.label_ts
    QUALIFY row_number() OVER (PARTITION BY sp.user_id, sp.label_ts ORDER BY p.valid_from DESC) = 1
)
SELECT sp.user_id, sp.label_ts, sp.churned,
       i.v AS known_{col}
FROM spine sp
LEFT JOIN int_plan_known i
  ON i.user_id = sp.user_id AND i.label_ts = sp.label_ts
