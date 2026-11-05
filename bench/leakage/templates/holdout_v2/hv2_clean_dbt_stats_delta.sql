-- leak: clean
-- param back: 1 | 7 | 14
-- param col: total_spend | n_orders
-- Growth between the newest snapshot before t and the newest snapshot before t - back days.
-- Both picks use a strict bound on updated_at at or before t.
WITH spine AS (
    SELECT user_id, label_ts, churned
    FROM analytics.marts.fct_training_spine
),
stg_stats AS (
    SELECT user_id, updated_at, {col} AS v
    FROM analytics.staging.stg_user_stats
),
snap_now AS (
    SELECT sp.user_id, sp.label_ts, max_by(st.v, st.updated_at) AS v_now
    FROM spine sp
    JOIN stg_stats st ON st.user_id = sp.user_id AND st.updated_at < sp.label_ts
    GROUP BY sp.user_id, sp.label_ts
),
snap_back AS (
    SELECT sp.user_id, sp.label_ts, max_by(st.v, st.updated_at) AS v_back
    FROM spine sp
    JOIN stg_stats st ON st.user_id = sp.user_id
                     AND st.updated_at < sp.label_ts - INTERVAL {back} DAY
    GROUP BY sp.user_id, sp.label_ts
)
SELECT sp.user_id, sp.label_ts, sp.churned,
       n.v_now,
       n.v_now - b.v_back AS {col}_delta_{back}d
FROM spine sp
LEFT JOIN snap_now n  ON n.user_id = sp.user_id AND n.label_ts = sp.label_ts
LEFT JOIN snap_back b ON b.user_id = sp.user_id AND b.label_ts = sp.label_ts
