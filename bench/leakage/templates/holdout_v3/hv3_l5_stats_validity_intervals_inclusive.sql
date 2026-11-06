-- leak: L5
-- param col: total_spend | n_orders
WITH stg_user_stats AS (
    SELECT user_id, updated_at AS computed_at, total_spend, n_orders
    FROM staging.stg_user_stats
),
int_stats_intervals AS (
    SELECT *,
           LEAD(computed_at) OVER (PARTITION BY user_id ORDER BY computed_at) AS superseded_at
    FROM stg_user_stats
),
spine AS (
    SELECT user_id, label_ts, churned FROM marts.fct_training_spine
)
SELECT sp.user_id, sp.label_ts, sp.churned, si.{col} AS stats_value
FROM spine sp
LEFT JOIN int_stats_intervals si
       ON si.user_id = sp.user_id
      AND si.computed_at <= sp.label_ts
      AND (si.superseded_at IS NULL OR si.superseded_at > sp.label_ts)
