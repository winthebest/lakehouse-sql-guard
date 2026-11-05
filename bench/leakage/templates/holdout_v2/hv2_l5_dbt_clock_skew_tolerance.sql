-- leak: L5
-- param skew_minutes: 1 | 30 | 180
-- A "clock skew tolerance" of a few minutes to hours (less than a day) on the load bound:
-- the midnight load at exactly t (and any load just after t) is counted.
WITH stg_events AS (
    SELECT event_id, user_id, event_ts AS purchased_at, ingested_at AS loaded_at, amount
    FROM staging.stg_events
),
stg_spine AS (
    SELECT user_id, label_ts, churned
    FROM marts.fct_training_spine
),
int_known_purchases AS (
    SELECT s.user_id, s.label_ts, e.event_id, e.amount
    FROM stg_spine s
    JOIN stg_events e
      ON e.user_id = s.user_id
     AND e.loaded_at < s.label_ts + INTERVAL {skew_minutes} MINUTE
     AND e.purchased_at >= s.label_ts - INTERVAL 30 DAY
)
SELECT s.user_id, s.label_ts, s.churned,
       COUNT(k.event_id)          AS n_purchases_30d,
       COALESCE(SUM(k.amount), 0) AS spend_30d
FROM stg_spine s
LEFT JOIN int_known_purchases k
  ON k.user_id = s.user_id AND k.label_ts = s.label_ts
GROUP BY s.user_id, s.label_ts, s.churned
