-- leak: clean
-- param k: 1 | 7
-- The delta is computed on the whole table, but LAG only looks at earlier stamps, and the
-- row picked for t is the newest one stamped strictly before t.
WITH stats_deltas AS (
    SELECT user_id, updated_at,
           total_spend - LAG(total_spend, {k}) OVER w AS spend_gain,
           n_orders    - LAG(n_orders, {k})    OVER w AS orders_gain
    FROM staging.stg_user_stats
    WINDOW w AS (PARTITION BY user_id ORDER BY updated_at)
)
SELECT l.user_id, l.label_ts, l.churned, d.spend_gain, d.orders_gain
FROM labels l
LEFT JOIN stats_deltas d
       ON d.user_id = l.user_id
      AND d.updated_at < l.label_ts
QUALIFY row_number() OVER (PARTITION BY l.user_id, l.label_ts
                           ORDER BY d.updated_at DESC NULLS LAST) = 1
