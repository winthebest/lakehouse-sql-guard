-- leak: L3
-- param rel: analytics.snapshots.plan_history_snapshot | plan_history
-- Aggregates every plan version of the user (counts, distinct plans, mean fee) over the whole
-- snapshot table, including versions that began after t.
WITH snap AS (
    SELECT user_id, plan, monthly_fee, valid_from
    FROM {rel}
),
plan_profile AS (
    SELECT user_id,
           COUNT(*)                          AS n_versions,
           COUNT(DISTINCT plan)              AS n_distinct_plans,
           AVG(monthly_fee)                  AS avg_fee,
           COUNT(*) FILTER (WHERE plan = 'pro') AS n_pro_versions
    FROM snap
    GROUP BY user_id
),
spine AS (
    SELECT user_id, label_ts, churned
    FROM analytics.marts.fct_training_spine
)
SELECT sp.user_id, sp.label_ts, sp.churned,
       pp.n_versions, pp.n_distinct_plans, pp.avg_fee, pp.n_pro_versions
FROM spine sp
LEFT JOIN plan_profile pp ON pp.user_id = sp.user_id
