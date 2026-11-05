-- leak: L1
-- param end_expr: MAX(label_ts) | MAX(label_ts) - INTERVAL 1 DAY | quantile_disc(label_ts, 0.9)
-- The upper bound comes from a CTE over the spine (the end of the training window), not
-- from t: rows earlier than the window end count purchases loaded after their own t.
WITH spine AS (
    SELECT user_id, label_ts, churned
    FROM labels
),
window_end AS (
    SELECT {end_expr} AS end_ts
    FROM spine
),
purchases AS (
    SELECT sp.user_id, sp.label_ts, e.event_id, e.amount
    FROM spine sp
    CROSS JOIN window_end w
    JOIN events e
      ON e.user_id = sp.user_id
     AND e.ingested_at < w.end_ts
     AND e.event_ts >= sp.label_ts - INTERVAL 30 DAY
)
SELECT sp.user_id, sp.label_ts, sp.churned,
       COUNT(p.event_id) AS n_purchases_in_window
FROM spine sp
LEFT JOIN purchases p
  ON p.user_id = sp.user_id AND p.label_ts = sp.label_ts
GROUP BY sp.user_id, sp.label_ts, sp.churned
