-- leak: L2
-- param rel: snapshots.plan_history_snapshot | analytics.snapshots.plan_history_snapshot | plan_history
-- param col: plan | monthly_fee
-- The dbt "current row" join (valid_to IS NULL) with no bound against t: it returns today's
-- open version for every spine row.
WITH spine AS (
    SELECT user_id, label_ts, churned
    FROM marts.fct_training_spine
),
current_plan AS (
    SELECT user_id, plan, monthly_fee
    FROM {rel}
    WHERE valid_to IS NULL
)
SELECT sp.user_id, sp.label_ts, sp.churned,
       cp.{col} AS current_{col}
FROM spine sp
LEFT JOIN current_plan cp ON cp.user_id = sp.user_id
