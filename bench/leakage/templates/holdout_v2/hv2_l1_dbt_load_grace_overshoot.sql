-- leak: L1
-- param grace: 1 | 3 | 7
-- param days: 14 | 30
-- A "load grace period" pushes the upper bound grace days (>= 1 day) past t, so purchases
-- made and loaded after t enter the feature.
WITH stg_events AS (
    SELECT event_id, user_id, event_ts AS purchased_at, ingested_at AS loaded_at, amount
    FROM analytics.staging.stg_events
),
stg_spine AS (
    SELECT user_id, label_ts, churned
    FROM analytics.marts.fct_training_spine
),
int_spine_purchases AS (
    SELECT s.user_id, s.label_ts, e.amount
    FROM stg_spine s
    JOIN stg_events e
      ON e.user_id = s.user_id
     AND e.loaded_at < s.label_ts + INTERVAL {grace} DAY
     AND e.purchased_at >= s.label_ts - INTERVAL {days} DAY
)
SELECT s.user_id, s.label_ts, s.churned,
       COUNT(i.amount)           AS n_purchases_{days}d,
       COALESCE(SUM(i.amount), 0) AS spend_{days}d
FROM stg_spine s
LEFT JOIN int_spine_purchases i
  ON i.user_id = s.user_id AND i.label_ts = s.label_ts
GROUP BY s.user_id, s.label_ts, s.churned
