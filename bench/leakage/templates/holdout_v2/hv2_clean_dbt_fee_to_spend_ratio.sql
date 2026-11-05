-- leak: clean
-- param months: 1 | 12
-- dbt mart joining two point-in-time intermediates: the newest plan version loaded before t
-- (no valid_to) and the newest stats snapshot stamped before t.
WITH spine AS (
    SELECT user_id, label_ts, churned,
           md5(CAST(user_id AS VARCHAR) || CAST(label_ts AS VARCHAR)) AS spine_sk
    FROM analytics.marts.fct_training_spine
),
stg_plans AS (
    SELECT user_id, plan, monthly_fee, valid_from, ingested_at AS loaded_at
    FROM analytics.snapshots.plan_history_snapshot
),
stg_stats AS (
    SELECT user_id, updated_at, total_spend
    FROM analytics.staging.stg_user_stats
),
int_plan_pit AS (
    SELECT sp.spine_sk, p.plan, p.monthly_fee
    FROM spine sp
    JOIN stg_plans p ON p.user_id = sp.user_id AND p.loaded_at < sp.label_ts
    QUALIFY row_number() OVER (PARTITION BY sp.spine_sk ORDER BY p.valid_from DESC) = 1
),
int_stats_pit AS (
    SELECT sp.spine_sk, max_by(s.total_spend, s.updated_at) AS lifetime_spend
    FROM spine sp
    JOIN stg_stats s ON s.user_id = sp.user_id AND s.updated_at < sp.label_ts
    GROUP BY sp.spine_sk
)
SELECT sp.user_id, sp.label_ts, sp.churned,
       ip.plan,
       ip.monthly_fee * {months} / NULLIF(st.lifetime_spend, 0) AS fee_to_spend_ratio
FROM spine sp
LEFT JOIN int_plan_pit ip  ON ip.spine_sk = sp.spine_sk
LEFT JOIN int_stats_pit st ON st.spine_sk = sp.spine_sk
