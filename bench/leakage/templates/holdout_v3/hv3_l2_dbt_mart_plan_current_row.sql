-- leak: L2
-- param w: 14 | 30
WITH stg_spine AS (
    SELECT user_id, label_ts, churned FROM analytics.marts.fct_training_spine
),
stg_entities AS (
    SELECT user_id, country, signup_ts FROM analytics.staging.stg_entities
),
stg_events AS (
    SELECT event_id, user_id, event_ts, ingested_at AS loaded_at, amount
    FROM analytics.staging.stg_events
),
stg_stats AS (
    SELECT user_id, updated_at AS computed_at, total_spend, n_orders
    FROM analytics.staging.stg_user_stats
),
stg_current_plans AS (
    -- current row of the snapshot
    SELECT user_id, plan, monthly_fee
    FROM analytics.snapshots.plan_history_snapshot
    WHERE valid_to IS NULL
),
int_event_features AS (
    SELECT sp.user_id, sp.label_ts,
           COUNT(ev.event_id)          AS n_orders_window,
           COALESCE(SUM(ev.amount), 0) AS spend_window
    FROM stg_spine sp
    LEFT JOIN stg_events ev
           ON ev.user_id = sp.user_id
          AND ev.loaded_at < sp.label_ts
          AND ev.event_ts >= sp.label_ts - INTERVAL {w} DAY
    GROUP BY sp.user_id, sp.label_ts
),
int_stats_features AS (
    SELECT sp.user_id, sp.label_ts, st.total_spend AS lifetime_spend, st.n_orders AS lifetime_orders
    FROM stg_spine sp
    ASOF LEFT JOIN stg_stats st
      ON st.user_id = sp.user_id
     AND sp.label_ts > st.computed_at
)
SELECT sp.user_id, sp.label_ts, sp.churned,
       en.country,
       date_diff('day', en.signup_ts, sp.label_ts) AS tenure_days,
       ef.n_orders_window, ef.spend_window,
       sf.lifetime_spend, sf.lifetime_orders,
       cp.plan, cp.monthly_fee
FROM stg_spine sp
LEFT JOIN stg_entities       en ON en.user_id = sp.user_id
LEFT JOIN int_event_features ef ON ef.user_id = sp.user_id AND ef.label_ts = sp.label_ts
LEFT JOIN int_stats_features sf ON sf.user_id = sp.user_id AND sf.label_ts = sp.label_ts
LEFT JOIN stg_current_plans  cp ON cp.user_id = sp.user_id
