-- leak: L2
-- param d: 3 | 7
-- "Only fresh stats": a lower bound, but nothing stops rows after t.
SELECT l.user_id, l.label_ts, l.churned, s.total_spend, s.n_orders, s.updated_at AS stats_ts
FROM labels l
LEFT JOIN user_stats s
       ON s.user_id = l.user_id
      AND s.updated_at >= l.label_ts - INTERVAL {d} DAY
QUALIFY row_number() OVER (PARTITION BY l.user_id, l.label_ts
                           ORDER BY s.updated_at DESC NULLS LAST) = 1
