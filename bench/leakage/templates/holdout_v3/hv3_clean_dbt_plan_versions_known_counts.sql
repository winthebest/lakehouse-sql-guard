-- leak: clean
-- Upgrades/downgrades among plan versions already loaded at t. The fee change is computed
-- against the previous version by valid_from, which is always loaded no later than its successor.
WITH stg_plans AS (
    SELECT user_id, plan, monthly_fee, valid_from, ingested_at,
           monthly_fee - LAG(monthly_fee) OVER (PARTITION BY user_id ORDER BY valid_from) AS fee_change
    FROM analytics.snapshots.plan_history_snapshot
),
spine AS (
    SELECT user_id, label_ts, churned FROM analytics.marts.fct_training_spine
),
int_plan_counts AS (
    SELECT sp.user_id, sp.label_ts, sp.churned,
           COUNT(p.user_id)                         AS n_versions_known,
           COUNT(*) FILTER (WHERE p.fee_change > 0) AS n_upgrades_known,
           COUNT(*) FILTER (WHERE p.fee_change < 0) AS n_downgrades_known
    FROM spine sp
    LEFT JOIN stg_plans p
           ON p.user_id = sp.user_id
          AND p.ingested_at < sp.label_ts
    GROUP BY ALL
)
SELECT * FROM int_plan_counts
