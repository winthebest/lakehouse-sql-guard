-- leak: clean
-- param days: 14 | 30 | 60
-- Two staging models unioned into one activity stream, each carrying its own load time;
-- the mart keeps only activity loaded before t (known_at < t).
WITH stg_purchases AS (
    SELECT user_id, event_ts AS activity_ts, ingested_at AS known_at, 'purchase' AS kind
    FROM staging.stg_events
),
stg_plan_changes AS (
    SELECT user_id, valid_from AS activity_ts, ingested_at AS known_at, 'plan_change' AS kind
    FROM snapshots.plan_history_snapshot
),
int_activity AS (
    SELECT * FROM stg_purchases
    UNION ALL
    SELECT * FROM stg_plan_changes
)
SELECT sp.user_id, sp.label_ts, sp.churned,
       COUNT(a.kind) FILTER (WHERE a.kind = 'purchase')    AS n_purchases_{days}d,
       COUNT(a.kind) FILTER (WHERE a.kind = 'plan_change') AS n_plan_changes_{days}d,
       date_diff('hour', MAX(a.activity_ts), sp.label_ts)  AS hours_since_last_activity
FROM marts.fct_training_spine sp
LEFT JOIN int_activity a
  ON a.user_id = sp.user_id
 AND a.known_at < sp.label_ts
 AND a.activity_ts >= sp.label_ts - INTERVAL {days} DAY
GROUP BY sp.user_id, sp.label_ts, sp.churned
