-- leak: L4
WITH snap AS (
    SELECT user_id, plan, monthly_fee, valid_from
    FROM snapshots.plan_history_snapshot
),
spine AS (
    SELECT user_id, label_ts, churned FROM marts.fct_training_spine
),
int_plan_pit AS (
    SELECT sp.user_id, sp.label_ts, sp.churned, s.plan, s.monthly_fee
    FROM spine sp
    LEFT JOIN snap s
           ON s.user_id = sp.user_id
          AND s.valid_from < sp.label_ts
    QUALIFY row_number() OVER (PARTITION BY sp.user_id, sp.label_ts
                               ORDER BY s.valid_from DESC NULLS LAST) = 1
)
SELECT user_id, label_ts, churned, plan, monthly_fee FROM int_plan_pit
