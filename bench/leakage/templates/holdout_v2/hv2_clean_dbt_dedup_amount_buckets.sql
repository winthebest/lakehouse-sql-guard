-- leak: clean
-- param days: 30 | 60
-- param hi: 20 | 50
-- Staging dedup keeps the FIRST load of each event_id; a later filter loaded_at < t then
-- keeps it exactly when some load of it happened before t. Conditional (pivot-like)
-- aggregation over the known purchases only.
WITH stg_events AS (
    SELECT event_id,
           user_id     AS customer_id,
           event_ts    AS purchased_at,
           ingested_at AS loaded_at,
           amount      AS order_amount
    FROM analytics.staging.stg_events
    QUALIFY row_number() OVER (PARTITION BY event_id ORDER BY ingested_at ASC) = 1
),
stg_spine AS (
    SELECT md5(CAST(user_id AS VARCHAR) || '|' || CAST(label_ts AS VARCHAR)) AS spine_key,
           user_id, label_ts, churned
    FROM analytics.marts.fct_training_spine
),
int_known_purchases AS (
    SELECT s.spine_key, e.order_amount
    FROM stg_spine s
    JOIN stg_events e
      ON e.customer_id = s.user_id
     AND e.loaded_at < s.label_ts
     AND e.purchased_at >= s.label_ts - INTERVAL {days} DAY
),
fct_purchase_buckets AS (
    SELECT spine_key,
           COUNT(*) FILTER (WHERE order_amount >= {hi})        AS n_large_orders,
           COUNT(*) FILTER (WHERE order_amount <  {hi})        AS n_small_orders,
           SUM(order_amount) FILTER (WHERE order_amount >= {hi}) AS large_order_spend
    FROM int_known_purchases
    GROUP BY spine_key
)
SELECT s.user_id, s.label_ts, s.churned,
       COALESCE(f.n_large_orders, 0)    AS n_large_orders,
       COALESCE(f.n_small_orders, 0)    AS n_small_orders,
       COALESCE(f.large_order_spend, 0) AS large_order_spend
FROM stg_spine s
LEFT JOIN fct_purchase_buckets f ON f.spine_key = s.spine_key
