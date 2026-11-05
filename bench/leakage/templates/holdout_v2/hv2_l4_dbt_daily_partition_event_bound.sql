-- leak: L4
-- param days: 7 | 28
-- dbt daily model keyed by purchase_date; the mart takes the days strictly before the date
-- of t. That bound is on event time only and less than 8 days before t, so purchases from
-- the previous days that arrived late were not loaded at t.
WITH stg_events AS (
    SELECT event_id, user_id AS customer_id, event_ts AS purchased_at, amount AS order_amount
    FROM analytics.staging.stg_events
),
stg_spine AS (
    SELECT md5(CAST(user_id AS VARCHAR) || '|' || CAST(label_ts AS VARCHAR)) AS spine_key,
           user_id, label_ts, churned
    FROM analytics.marts.fct_training_spine
),
int_daily_purchases AS (
    SELECT customer_id, CAST(purchased_at AS DATE) AS purchase_date,
           SUM(order_amount) AS daily_spend, COUNT(*) AS daily_orders
    FROM stg_events
    GROUP BY customer_id, CAST(purchased_at AS DATE)
),
fct_spend AS (
    SELECT s.spine_key, SUM(d.daily_spend) AS spend, SUM(d.daily_orders) AS orders
    FROM stg_spine s
    JOIN int_daily_purchases d
      ON d.customer_id = s.user_id
     AND d.purchase_date <  CAST(s.label_ts AS DATE)
     AND d.purchase_date >= CAST(s.label_ts AS DATE) - INTERVAL {days} DAY
    GROUP BY s.spine_key
)
SELECT s.user_id, s.label_ts, s.churned,
       COALESCE(f.spend, 0)  AS spend_{days}d,
       COALESCE(f.orders, 0) AS orders_{days}d
FROM stg_spine s
LEFT JOIN fct_spend f ON f.spine_key = s.spine_key
