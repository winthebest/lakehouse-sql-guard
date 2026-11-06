-- leak: clean
WITH spine AS (
    SELECT user_id, label_ts, churned FROM analytics.marts.fct_training_spine
),
ranked AS (
    SELECT sp.user_id, sp.label_ts, p.plan, p.monthly_fee,
           row_number() OVER (PARTITION BY sp.user_id, sp.label_ts
                              ORDER BY p.valid_from DESC, p.ingested_at DESC) AS version_rank
    FROM spine sp
    JOIN analytics.snapshots.plan_history_snapshot p
      ON p.user_id = sp.user_id
     AND p.ingested_at < sp.label_ts
),
pivoted AS (
    SELECT user_id, label_ts,
           MAX(plan)        FILTER (WHERE version_rank = 1) AS plan_now,
           MAX(plan)        FILTER (WHERE version_rank = 2) AS plan_before,
           MAX(monthly_fee) FILTER (WHERE version_rank = 1) AS fee_now,
           MAX(monthly_fee) FILTER (WHERE version_rank = 2) AS fee_before
    FROM ranked
    WHERE version_rank <= 2
    GROUP BY user_id, label_ts
)
SELECT sp.user_id, sp.label_ts, sp.churned,
       pv.plan_now, pv.plan_before,
       pv.fee_now - pv.fee_before AS fee_delta,
       pv.plan_before IS NOT NULL AND pv.fee_now > pv.fee_before AS upgraded_last_change
FROM spine sp
LEFT JOIN pivoted pv ON pv.user_id = sp.user_id AND pv.label_ts = sp.label_ts
