-- leak: clean
-- param col: total_spend | n_orders
-- param fn: percent_rank() | cume_dist()
-- The rank is computed inside one midnight snapshot (PARTITION BY updated_at); the snapshot
-- picked for a row is stamped before t, and so is every row it is ranked against.
WITH ranked_snapshots AS (
    SELECT user_id, updated_at,
           {fn} OVER (PARTITION BY updated_at ORDER BY {col}) AS pct
    FROM user_stats
)
SELECT l.user_id, l.label_ts, l.churned,
       r.pct AS {col}_pctile_in_snapshot
FROM labels l
LEFT JOIN ranked_snapshots r
  ON r.user_id = l.user_id
 AND r.updated_at < l.label_ts
QUALIFY row_number() OVER (PARTITION BY l.user_id, l.label_ts ORDER BY r.updated_at DESC) = 1
