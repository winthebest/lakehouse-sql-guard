-- leak: clean
WITH stg_events AS (
    SELECT event_id, user_id, event_ts, ingested_at, amount
    FROM analytics.staging.stg_events
),
int_load_batches AS (
    SELECT user_id, ingested_at, SUM(amount) AS batch_amount, COUNT(*) AS batch_orders
    FROM stg_events
    GROUP BY user_id, ingested_at
),
int_cumulative AS (
    SELECT user_id, ingested_at,
           SUM(batch_amount) OVER (PARTITION BY user_id ORDER BY ingested_at) AS cum_spend,
           SUM(batch_orders) OVER (PARTITION BY user_id ORDER BY ingested_at) AS cum_orders
    FROM int_load_batches
),
spine AS (
    SELECT user_id, label_ts, churned FROM analytics.marts.fct_training_spine
)
SELECT sp.user_id, sp.label_ts, sp.churned, c.cum_spend, c.cum_orders
FROM spine sp
ASOF LEFT JOIN int_cumulative c
  ON c.user_id = sp.user_id
 AND sp.label_ts > c.ingested_at
