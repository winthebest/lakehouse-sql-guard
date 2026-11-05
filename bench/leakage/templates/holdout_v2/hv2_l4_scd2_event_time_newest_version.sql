-- leak: L4
-- param col: plan | monthly_fee
-- param rel: snapshots.plan_history_snapshot | plan_history
-- Newest version with valid_from < t, but no ingested_at bound: a plan change that took
-- effect shortly before t may have been loaded up to 7 nights later.
WITH spine AS (
    SELECT user_id, label_ts, churned
    FROM marts.fct_training_spine
),
stg_plans AS (
    SELECT user_id, plan, monthly_fee, valid_from
    FROM {rel}
),
int_plan_at_t AS (
    SELECT sp.user_id, sp.label_ts, p.{col} AS v
    FROM spine sp
    JOIN stg_plans p
      ON p.user_id = sp.user_id
     AND p.valid_from < sp.label_ts
    QUALIFY row_number() OVER (PARTITION BY sp.user_id, sp.label_ts ORDER BY p.valid_from DESC) = 1
)
SELECT sp.user_id, sp.label_ts, sp.churned,
       i.v AS plan_{col}_at_t
FROM spine sp
LEFT JOIN int_plan_at_t i
  ON i.user_id = sp.user_id AND i.label_ts = sp.label_ts
