-- leak: clean
WITH spine AS (
    SELECT user_id, label_ts, churned FROM marts.fct_training_spine
),
stg_events AS (
    SELECT user_id, event_ts, ingested_at, amount FROM staging.stg_events
),
stg_plans AS (
    SELECT user_id, plan, monthly_fee, valid_from, ingested_at FROM snapshots.plan_history_snapshot
),
int_spend AS (
    SELECT sp.user_id, sp.label_ts, COALESCE(SUM(e.amount), 0) AS spend_30d
    FROM spine sp
    LEFT JOIN stg_events e
           ON e.user_id = sp.user_id
          AND e.ingested_at < sp.label_ts
          AND e.event_ts >= sp.label_ts - INTERVAL 30 DAY
    GROUP BY sp.user_id, sp.label_ts
),
int_plan AS (
    SELECT sp.user_id, sp.label_ts, p.plan, p.monthly_fee
    FROM spine sp
    LEFT JOIN stg_plans p
           ON p.user_id = sp.user_id
          AND p.ingested_at < sp.label_ts
    QUALIFY row_number() OVER (PARTITION BY sp.user_id, sp.label_ts
                               ORDER BY p.valid_from DESC NULLS LAST) = 1
)
SELECT sp.user_id, sp.label_ts, sp.churned,
       ip.plan,
       s.spend_30d,
       s.spend_30d / NULLIF(ip.monthly_fee, 0) AS spend_to_fee_ratio
FROM spine sp
JOIN int_spend s      ON s.user_id = sp.user_id  AND s.label_ts = sp.label_ts
LEFT JOIN int_plan ip ON ip.user_id = sp.user_id AND ip.label_ts = sp.label_ts
