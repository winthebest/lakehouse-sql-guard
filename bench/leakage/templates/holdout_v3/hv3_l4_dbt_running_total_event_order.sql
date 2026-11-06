-- leak: L4
WITH stg_events AS (
    SELECT event_id, user_id, event_ts, ingested_at, amount
    FROM analytics.staging.stg_events
),
int_running AS (
    SELECT *,
           SUM(amount)  OVER (PARTITION BY user_id ORDER BY event_ts, event_id
                              ROWS UNBOUNDED PRECEDING) AS running_spend,
           row_number() OVER (PARTITION BY user_id ORDER BY event_ts, event_id) AS order_seq
    FROM stg_events
),
spine AS (
    SELECT user_id, label_ts, churned FROM analytics.marts.fct_training_spine
)
SELECT sp.user_id, sp.label_ts, sp.churned,
       r.running_spend AS lifetime_spend_at_last_known,
       r.order_seq     AS lifetime_orders_at_last_known
FROM spine sp
LEFT JOIN int_running r
       ON r.user_id = sp.user_id
      AND r.ingested_at < sp.label_ts
QUALIFY row_number() OVER (PARTITION BY sp.user_id, sp.label_ts
                           ORDER BY r.event_ts DESC NULLS LAST, r.event_id DESC NULLS LAST) = 1
