-- leak: L2
-- param stats_rel: analytics.staging.stg_user_stats | staging.stg_user_stats
-- param join: LEFT JOIN | JOIN
-- A current-state dim_users (the newest stats row per user, as of today) joined to the
-- spine with no time condition: the row chosen comes after t for every older spine row.
WITH stg_user_stats AS (
    SELECT user_id, updated_at, total_spend, n_orders
    FROM {stats_rel}
),
dim_users AS (
    SELECT u.user_id, en.country,
           u.total_spend AS lifetime_spend,
           u.n_orders    AS lifetime_orders
    FROM stg_user_stats u
    JOIN analytics.staging.stg_entities en ON en.user_id = u.user_id
    QUALIFY row_number() OVER (PARTITION BY u.user_id ORDER BY u.updated_at DESC) = 1
),
spine AS (
    SELECT user_id, label_ts, churned
    FROM marts.fct_training_spine
)
SELECT sp.user_id, sp.label_ts, sp.churned,
       d.country, d.lifetime_spend, d.lifetime_orders
FROM spine sp
{join} dim_users d ON d.user_id = sp.user_id
