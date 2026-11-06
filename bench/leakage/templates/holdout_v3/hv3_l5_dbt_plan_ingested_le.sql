-- leak: L5
-- param rhs: sp.label_ts | sp.label_ts + INTERVAL 18 HOUR
WITH spine AS (
    SELECT user_id, label_ts, churned FROM marts.fct_training_spine
),
snap AS (
    SELECT user_id, plan, monthly_fee, valid_from, ingested_at
    FROM snapshots.plan_history_snapshot
),
int_plan_pit AS (
    SELECT sp.user_id, sp.label_ts, sp.churned, sn.plan, sn.monthly_fee
    FROM spine sp
    LEFT JOIN snap sn
           ON sn.user_id = sp.user_id
          AND sn.ingested_at <= {rhs}
    QUALIFY row_number() OVER (PARTITION BY sp.user_id, sp.label_ts
                               ORDER BY sn.valid_from DESC NULLS LAST) = 1
)
SELECT * FROM int_plan_pit
