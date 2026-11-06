-- leak: L6
WITH spine AS (
    SELECT user_id, label_ts, churned FROM analytics.marts.fct_training_spine
),
stg_outcomes AS (
    SELECT user_id, label_ts, refund_after_churn FROM analytics.staging.stg_user_outcomes
),
stg_events AS (
    SELECT user_id, event_ts, ingested_at, amount FROM analytics.staging.stg_events
),
int_spend AS (
    SELECT sp.user_id, sp.label_ts, COALESCE(SUM(e.amount), 0) AS spend_30d
    FROM spine sp
    LEFT JOIN stg_events e
           ON e.user_id = sp.user_id
          AND e.ingested_at < sp.label_ts
          AND e.event_ts >= sp.label_ts - INTERVAL 30 DAY
    GROUP BY sp.user_id, sp.label_ts
)
SELECT sp.user_id, sp.label_ts, sp.churned,
       s.spend_30d,
       COALESCE(o.refund_after_churn, 0) AS refund_amount,
       o.refund_after_churn > 0          AS had_refund
FROM spine sp
LEFT JOIN int_spend s     ON s.user_id = sp.user_id AND s.label_ts = sp.label_ts
LEFT JOIN stg_outcomes o  ON o.user_id = sp.user_id AND o.label_ts = sp.label_ts
