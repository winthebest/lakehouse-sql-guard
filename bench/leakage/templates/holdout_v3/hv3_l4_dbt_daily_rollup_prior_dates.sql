-- leak: L4
-- param days: 7 | 14
WITH stg_events AS (
    SELECT user_id, CAST(event_ts AS DATE) AS purchase_date, amount
    FROM staging.stg_events
),
int_daily_spend AS (
    SELECT user_id, purchase_date, SUM(amount) AS daily_spend, COUNT(*) AS daily_orders
    FROM stg_events
    GROUP BY user_id, purchase_date
),
spine AS (
    SELECT user_id, label_ts, CAST(label_ts AS DATE) AS label_date, churned
    FROM marts.fct_training_spine
)
SELECT sp.user_id, sp.label_ts, sp.churned,
       COUNT(d.purchase_date) AS active_days,
       SUM(d.daily_spend)     AS spend,
       MAX(d.daily_orders)    AS max_daily_orders
FROM spine sp
LEFT JOIN int_daily_spend d
       ON d.user_id = sp.user_id
      AND d.purchase_date <  sp.label_date
      AND d.purchase_date >= sp.label_date - {days}
GROUP BY ALL
