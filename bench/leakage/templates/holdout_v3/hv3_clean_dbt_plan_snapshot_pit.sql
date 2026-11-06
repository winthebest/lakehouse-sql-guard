-- leak: clean
-- Point-in-time SCD2 join: newest version already loaded at t, ranked by valid_from.
-- valid_to is deliberately not used (its value today may come from a later load).
WITH spine AS (
    SELECT user_id, label_ts, churned FROM marts.fct_training_spine
),
snap AS (
    SELECT user_id, plan, monthly_fee, valid_from, ingested_at
    FROM snapshots.plan_history_snapshot
),
int_plan_pit AS (
    SELECT sp.user_id, sp.label_ts, sp.churned, sn.plan, sn.monthly_fee, sn.valid_from
    FROM spine sp
    LEFT JOIN snap sn
           ON sn.user_id = sp.user_id
          AND sn.ingested_at < sp.label_ts
    QUALIFY row_number() OVER (PARTITION BY sp.user_id, sp.label_ts
                               ORDER BY sn.valid_from DESC NULLS LAST,
                                        sn.ingested_at DESC NULLS LAST) = 1
)
SELECT user_id, label_ts, churned, plan, monthly_fee,
       date_diff('day', valid_from, label_ts) AS days_on_plan
FROM int_plan_pit
