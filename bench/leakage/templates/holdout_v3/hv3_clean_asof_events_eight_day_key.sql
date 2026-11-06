-- leak: clean
-- param gap: 8 | 14
WITH spine AS (
    SELECT user_id, label_ts, churned, label_ts - INTERVAL {gap} DAY AS settled_ts
    FROM labels
)
SELECT sp.user_id, sp.label_ts, sp.churned,
       e.event_ts                                AS last_settled_purchase_ts,
       date_diff('hour', e.event_ts, sp.label_ts) AS hours_since_settled_purchase
FROM spine sp
ASOF LEFT JOIN analytics.staging.stg_events e
  ON e.user_id = sp.user_id
 AND sp.settled_ts > e.event_ts
