-- leak: L1
-- param unit: hour | day
-- One spine x purchases intermediate, two FILTERed aggregates. The purchase count is
-- point-in-time (loaded_at < t), but the time to the next purchase reads the first purchase
-- with purchased_at >= t: the next event, which is in the future.
WITH stg_events AS (
    SELECT user_id AS customer_id, event_ts AS purchased_at, ingested_at AS loaded_at, amount
    FROM staging.stg_events
),
stg_spine AS (
    SELECT user_id, label_ts, churned
    FROM marts.fct_training_spine
),
int_spine_x_purchases AS (
    SELECT s.user_id, s.label_ts, s.churned, e.purchased_at, e.loaded_at, e.amount
    FROM stg_spine s
    LEFT JOIN stg_events e ON e.customer_id = s.user_id
)
SELECT user_id, label_ts, churned,
       COUNT(amount) FILTER (WHERE loaded_at < label_ts) AS n_known_purchases,
       date_diff('{unit}', label_ts,
                 MIN(purchased_at) FILTER (WHERE purchased_at >= label_ts)) AS time_to_next_purchase
FROM int_spine_x_purchases
GROUP BY user_id, label_ts, churned
