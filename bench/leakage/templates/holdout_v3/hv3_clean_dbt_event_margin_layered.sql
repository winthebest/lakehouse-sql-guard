-- leak: clean
-- param gap: 8 | 12
WITH stg_events AS (
    SELECT event_id, user_id, event_ts AS purchased_at, amount
    FROM staging.stg_events
),
spine AS (
    SELECT user_id, label_ts, churned,
           label_ts - INTERVAL {gap} DAY AS settled_before
    FROM marts.fct_training_spine
),
int_settled AS (
    SELECT sp.user_id, sp.label_ts,
           COUNT(e.event_id)    AS n_settled_orders_28d,
           SUM(e.amount)        AS settled_spend_28d,
           MAX(e.purchased_at)  AS last_settled_purchase
    FROM spine sp
    LEFT JOIN stg_events e
           ON e.user_id = sp.user_id
          AND e.purchased_at <  sp.settled_before
          AND e.purchased_at >= sp.settled_before - INTERVAL 28 DAY
    GROUP BY sp.user_id, sp.label_ts
)
SELECT sp.user_id, sp.label_ts, sp.churned,
       i.n_settled_orders_28d, i.settled_spend_28d,
       date_diff('hour', i.last_settled_purchase, sp.label_ts) AS hours_since_settled_purchase
FROM spine sp
LEFT JOIN int_settled i ON i.user_id = sp.user_id AND i.label_ts = sp.label_ts
