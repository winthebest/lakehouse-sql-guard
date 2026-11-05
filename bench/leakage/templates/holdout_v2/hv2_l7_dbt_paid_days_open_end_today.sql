-- leak: L7
-- param unit: day | week
-- Paid time over the plan versions known at t (ingested_at < t). Each span ends at the next
-- known version (LEAD within the known set, also known at t), but the open span of the newest
-- known version is closed with current_date, so the total depends on the run date.
WITH spine AS (
    SELECT user_id, label_ts, churned
    FROM analytics.marts.fct_training_spine
),
stg_plans AS (
    SELECT user_id, plan, monthly_fee, valid_from, ingested_at
    FROM analytics.snapshots.plan_history_snapshot
),
int_known_versions AS (
    SELECT sp.user_id, sp.label_ts, p.monthly_fee, p.valid_from,
           LEAD(p.valid_from) OVER (PARTITION BY sp.user_id, sp.label_ts ORDER BY p.valid_from) AS next_known_from
    FROM spine sp
    JOIN stg_plans p
      ON p.user_id = sp.user_id
     AND p.ingested_at < sp.label_ts
),
int_paid_spans AS (
    SELECT user_id, label_ts,
           date_diff('{unit}', valid_from,
                     COALESCE(next_known_from, CAST(current_date AS TIMESTAMP))) AS span
    FROM int_known_versions
    WHERE monthly_fee > 0
)
SELECT sp.user_id, sp.label_ts, sp.churned,
       COALESCE(SUM(ps.span), 0) AS paid_{unit}s_total
FROM spine sp
LEFT JOIN int_paid_spans ps
  ON ps.user_id = sp.user_id AND ps.label_ts = sp.label_ts
GROUP BY sp.user_id, sp.label_ts, sp.churned
