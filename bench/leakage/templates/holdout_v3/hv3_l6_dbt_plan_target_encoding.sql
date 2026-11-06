-- leak: L6
WITH spine AS (
    SELECT user_id, label_ts, churned FROM analytics.marts.fct_training_spine
),
stg_plans AS (
    SELECT user_id, plan, valid_from, ingested_at FROM analytics.snapshots.plan_history_snapshot
),
int_plan_pit AS (
    SELECT sp.user_id, sp.label_ts, sp.churned, p.plan
    FROM spine sp
    LEFT JOIN stg_plans p
           ON p.user_id = sp.user_id
          AND p.ingested_at < sp.label_ts
    QUALIFY row_number() OVER (PARTITION BY sp.user_id, sp.label_ts
                               ORDER BY p.valid_from DESC NULLS LAST) = 1
),
int_plan_rates AS (
    SELECT plan, AVG(churned) AS plan_churn_rate FROM int_plan_pit GROUP BY plan
)
SELECT pp.user_id, pp.label_ts, pp.churned, pp.plan, pr.plan_churn_rate
FROM int_plan_pit pp
LEFT JOIN int_plan_rates pr ON pr.plan IS NOT DISTINCT FROM pp.plan
