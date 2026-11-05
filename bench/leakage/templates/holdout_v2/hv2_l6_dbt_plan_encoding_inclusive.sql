-- leak: L6
-- param prior: 0.0 | 0.2
-- The plan itself is point-in-time (newest version loaded before t), but the plan-level churn
-- encoding is a running mean whose frame ends at CURRENT ROW: it sees the row's own label.
WITH spine AS (
    SELECT user_id, label_ts, churned
    FROM analytics.marts.fct_training_spine
),
stg_plans AS (
    SELECT user_id, plan, valid_from, ingested_at
    FROM analytics.snapshots.plan_history_snapshot
),
int_plan_pit AS (
    SELECT sp.user_id, sp.label_ts, p.plan
    FROM spine sp
    JOIN stg_plans p ON p.user_id = sp.user_id AND p.ingested_at < sp.label_ts
    QUALIFY row_number() OVER (PARTITION BY sp.user_id, sp.label_ts ORDER BY p.valid_from DESC) = 1
),
spine_plan AS (
    SELECT sp.user_id, sp.label_ts, sp.churned, COALESCE(ip.plan, 'unknown') AS plan
    FROM spine sp
    LEFT JOIN int_plan_pit ip ON ip.user_id = sp.user_id AND ip.label_ts = sp.label_ts
)
SELECT user_id, label_ts, churned, plan,
       (SUM(churned) OVER w + {prior}) / (COUNT(*) OVER w + 1) AS plan_churn_rate_to_date
FROM spine_plan
WINDOW w AS (PARTITION BY plan ORDER BY label_ts, user_id
             ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
