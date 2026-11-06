-- leak: L1
-- param d: 7 | 14
WITH stg_plans AS (
    SELECT user_id, plan, monthly_fee, valid_from, ingested_at
    FROM analytics.snapshots.plan_history_snapshot
),
spine AS (
    SELECT user_id, label_ts, churned FROM analytics.marts.fct_training_spine
)
SELECT sp.user_id, sp.label_ts, sp.churned,
       p.plan        AS plan_at_horizon,
       p.monthly_fee AS fee_at_horizon
FROM spine sp
LEFT JOIN stg_plans p
       ON p.user_id = sp.user_id
      AND p.ingested_at < sp.label_ts + INTERVAL {d} DAY
QUALIFY row_number() OVER (PARTITION BY sp.user_id, sp.label_ts
                           ORDER BY p.valid_from DESC NULLS LAST) = 1
