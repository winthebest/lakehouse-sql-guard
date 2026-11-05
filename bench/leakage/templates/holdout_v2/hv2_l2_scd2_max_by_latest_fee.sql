-- leak: L2
-- param rel: plan_history | snapshots.plan_history_snapshot
-- max_by(..., valid_from) per user over all versions: the newest version in today's table.
WITH stg_plans AS (
    SELECT user_id, plan, monthly_fee, valid_from
    FROM {rel}
),
latest_plan AS (
    SELECT user_id,
           max_by(plan, valid_from)        AS plan_latest,
           max_by(monthly_fee, valid_from) AS fee_latest
    FROM stg_plans
    GROUP BY user_id
)
SELECT l.user_id, l.label_ts, l.churned,
       lp.plan_latest,
       lp.fee_latest,
       lp.plan_latest = 'pro' AS is_pro
FROM labels l
LEFT JOIN latest_plan lp ON lp.user_id = l.user_id
