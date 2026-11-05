-- leak: L4
-- param n: 3 | 5
-- param stat: AVG | MEDIAN
-- Top-N most recent purchases by event time (QUALIFY row_number <= n) with LAG gaps, bounded
-- only by event_ts < t. events has an ingestion time, and the newest purchases before t may
-- not have been loaded yet at t.
WITH stg_events AS (
    SELECT event_id, user_id, event_ts, amount
    FROM analytics.staging.stg_events
),
spine AS (
    SELECT user_id, label_ts, churned
    FROM analytics.marts.fct_training_spine
),
int_recent_n AS (
    SELECT sp.user_id, sp.label_ts, e.amount,
           date_diff('minute',
                     LAG(e.event_ts) OVER (PARTITION BY sp.user_id, sp.label_ts ORDER BY e.event_ts, e.event_id),
                     e.event_ts) AS gap_minutes
    FROM spine sp
    JOIN stg_events e
      ON e.user_id = sp.user_id
     AND e.event_ts < sp.label_ts
    QUALIFY row_number() OVER (PARTITION BY sp.user_id, sp.label_ts
                               ORDER BY e.event_ts DESC, e.event_id DESC) <= {n}
)
SELECT sp.user_id, sp.label_ts, sp.churned,
       {stat}(r.amount)     AS recent_ticket_stat,
       AVG(r.gap_minutes)   AS mean_gap_minutes
FROM spine sp
LEFT JOIN int_recent_n r
  ON r.user_id = sp.user_id AND r.label_ts = sp.label_ts
GROUP BY sp.user_id, sp.label_ts, sp.churned
