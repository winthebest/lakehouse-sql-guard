-- leak: L2
WITH snap AS (
    SELECT user_id, plan, monthly_fee, valid_from, ingested_at
    FROM snapshots.plan_history_snapshot
),
stg_current_plan AS (
    SELECT user_id, plan AS current_plan, monthly_fee AS current_fee
    FROM snap
    QUALIFY row_number() OVER (PARTITION BY user_id ORDER BY ingested_at DESC, valid_from DESC) = 1
),
spine AS (
    SELECT user_id, label_ts, churned FROM marts.fct_training_spine
)
SELECT sp.user_id, sp.label_ts, sp.churned,
       cp.current_plan, cp.current_fee,
       cp.current_plan = 'free' AS is_free
FROM spine sp
LEFT JOIN stg_current_plan cp ON cp.user_id = sp.user_id
