-- leak: L5
-- param days: 7 | 30
-- param agg: SUM(e.amount) | COUNT(e.event_id)
-- BETWEEN is inclusive at the top: purchases loaded at exactly t (the midnight load, when t
-- is a midnight) are counted, but the strict boundary says they are not known at t.
SELECT l.user_id, l.label_ts, l.churned,
       COALESCE({agg}, 0) AS loaded_purchase_feature_{days}d
FROM labels l
LEFT JOIN events e
  ON e.user_id = l.user_id
 AND e.ingested_at BETWEEN l.label_ts - INTERVAL {days} DAY AND l.label_ts
GROUP BY l.user_id, l.label_ts, l.churned
