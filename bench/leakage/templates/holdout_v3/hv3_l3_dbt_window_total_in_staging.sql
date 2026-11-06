-- leak: L3
WITH stg_events AS (
    SELECT event_id, user_id, event_ts, ingested_at, amount,
           SUM(amount) OVER (PARTITION BY user_id) AS user_lifetime_spend,
           COUNT(*)    OVER (PARTITION BY user_id) AS user_lifetime_orders
    FROM staging.stg_events
),
spine AS (
    SELECT user_id, label_ts, churned FROM marts.fct_training_spine
),
int_known AS (
    SELECT sp.user_id, sp.label_ts, sp.churned, e.amount, e.user_lifetime_spend, e.user_lifetime_orders
    FROM spine sp
    LEFT JOIN stg_events e
           ON e.user_id = sp.user_id
          AND e.ingested_at < sp.label_ts
          AND e.event_ts >= sp.label_ts - INTERVAL 30 DAY
)
SELECT user_id, label_ts, churned,
       SUM(amount)               AS spend_30d,
       MAX(user_lifetime_spend)  AS lifetime_spend,
       MAX(user_lifetime_orders) AS lifetime_orders,
       SUM(amount) / NULLIF(MAX(user_lifetime_spend), 0) AS recent_share
FROM int_known
GROUP BY ALL
