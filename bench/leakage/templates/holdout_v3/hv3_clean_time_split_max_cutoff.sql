-- leak: clean
-- param back: 3 | 6
-- Hold out the last days of prediction times; the cutoff only selects training rows.
SELECT l.user_id, l.label_ts, l.churned,
       (SELECT s.n_orders
          FROM user_stats s
         WHERE s.user_id = l.user_id
           AND s.updated_at < l.label_ts
         ORDER BY s.updated_at DESC
         LIMIT 1) AS n_orders_known
FROM labels l
WHERE l.label_ts <= (SELECT MAX(label_ts) FROM labels) - INTERVAL {back} DAY
