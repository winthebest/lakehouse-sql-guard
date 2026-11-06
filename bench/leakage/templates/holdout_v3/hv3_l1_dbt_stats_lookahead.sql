-- leak: L1
-- param d: 2 | 7
-- "Allow for recompute delay" by looking d days past t: the newest row is then after t.
WITH stg_stats AS (
    SELECT user_id, updated_at, total_spend, n_orders FROM staging.stg_user_stats
),
spine AS (
    SELECT user_id, label_ts, churned FROM marts.fct_training_spine
),
int_stats_pit AS (
    SELECT sp.user_id, sp.label_ts, sp.churned, st.total_spend, st.n_orders
    FROM spine sp
    LEFT JOIN stg_stats st
           ON st.user_id = sp.user_id
          AND st.updated_at < sp.label_ts + INTERVAL {d} DAY
    QUALIFY row_number() OVER (PARTITION BY sp.user_id, sp.label_ts
                               ORDER BY st.updated_at DESC NULLS LAST) = 1
)
SELECT * FROM int_stats_pit
