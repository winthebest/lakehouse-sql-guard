-- leak: L3
WITH stg_plans AS (
    SELECT user_id, plan, monthly_fee, valid_from
    FROM analytics.snapshots.plan_history_snapshot
),
int_plan_summary AS (
    SELECT user_id,
           COUNT(*)             AS n_plan_versions,
           COUNT(DISTINCT plan) AS n_distinct_plans,
           AVG(monthly_fee)     AS avg_fee
    FROM stg_plans
    GROUP BY user_id
),
spine AS (
    SELECT user_id, label_ts, churned FROM analytics.marts.fct_training_spine
)
SELECT sp.user_id, sp.label_ts, sp.churned,
       ps.n_plan_versions, ps.n_distinct_plans, ps.avg_fee
FROM spine sp
LEFT JOIN int_plan_summary ps ON ps.user_id = sp.user_id
