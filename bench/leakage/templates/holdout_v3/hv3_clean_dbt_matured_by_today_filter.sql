-- leak: clean
WITH spine AS (
    -- only prediction times whose label had matured by the day the model is built
    SELECT user_id, label_ts, churned
    FROM marts.fct_training_spine
    WHERE label_ts + INTERVAL 14 DAY < current_date
),
stats_pit AS (
    SELECT sp.user_id, sp.label_ts, s.total_spend, s.n_orders
    FROM spine sp
    ASOF LEFT JOIN staging.stg_user_stats s
      ON s.user_id = sp.user_id
     AND sp.label_ts > s.updated_at
),
plan_pit AS (
    SELECT sp.user_id, sp.label_ts, p.plan
    FROM spine sp
    LEFT JOIN snapshots.plan_history_snapshot p
           ON p.user_id = sp.user_id
          AND p.ingested_at < sp.label_ts
    QUALIFY row_number() OVER (PARTITION BY sp.user_id, sp.label_ts
                               ORDER BY p.valid_from DESC NULLS LAST) = 1
)
SELECT sp.user_id, sp.label_ts, sp.churned, st.total_spend, st.n_orders, pp.plan
FROM spine sp
LEFT JOIN stats_pit st ON st.user_id = sp.user_id AND st.label_ts = sp.label_ts
LEFT JOIN plan_pit  pp ON pp.user_id = sp.user_id AND pp.label_ts = sp.label_ts
