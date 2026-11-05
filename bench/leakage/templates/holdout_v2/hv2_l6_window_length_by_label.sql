-- leak: L6
-- param churn_days: 3 | 7
-- param stay_days: 30 | 60
-- The purchases are point-in-time, but the length of the window is chosen by the row's own
-- churned: the feature value depends on the label.
SELECT l.user_id, l.label_ts, l.churned,
       COUNT(e.event_id) AS n_purchases_window
FROM labels l
LEFT JOIN events e
  ON e.user_id = l.user_id
 AND e.ingested_at < l.label_ts
 AND e.event_ts >= l.label_ts - CASE WHEN l.churned = 1 THEN INTERVAL {churn_days} DAY
                                     ELSE INTERVAL {stay_days} DAY END
GROUP BY l.user_id, l.label_ts, l.churned
