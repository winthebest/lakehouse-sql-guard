-- leak: clean
-- param col: total_spend | n_orders
-- Unlike an SCD2 valid_to, the end of a stats interval cannot hide a known row: every row
-- after the newest one stamped before t is itself stamped at or after t.
WITH stg_user_stats AS (
    SELECT user_id, updated_at AS computed_at, total_spend, n_orders
    FROM analytics.staging.stg_user_stats
),
int_stats_intervals AS (
    SELECT *,
           LEAD(computed_at) OVER (PARTITION BY user_id ORDER BY computed_at) AS superseded_at
    FROM stg_user_stats
),
spine AS (
    SELECT user_id, label_ts, churned FROM analytics.marts.fct_training_spine
)
SELECT sp.user_id, sp.label_ts, sp.churned,
       si.{col}                                     AS stats_value,
       date_diff('hour', si.computed_at, sp.label_ts) AS stats_age_hours
FROM spine sp
LEFT JOIN int_stats_intervals si
       ON si.user_id = sp.user_id
      AND si.computed_at < sp.label_ts
      AND (si.superseded_at IS NULL OR si.superseded_at >= sp.label_ts)
