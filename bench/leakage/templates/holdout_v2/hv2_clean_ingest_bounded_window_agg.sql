-- leak: clean
-- param days: 7 | 30 | 90
-- param agg: SUM(e.amount) | COUNT(e.event_id) | AVG(e.amount)
-- A purchase counts only once it was loaded (ingested_at < label_ts). The lower bound on
-- event_ts only narrows the window and never reaches past t.
SELECT l.user_id, l.label_ts, l.churned,
       COALESCE(f.feat, 0) AS purchase_feature_{days}d
FROM labels l
LEFT JOIN (
    SELECT l2.user_id, l2.label_ts, {agg} AS feat
    FROM labels l2
    JOIN events e
      ON e.user_id = l2.user_id
     AND e.ingested_at < l2.label_ts
     AND e.event_ts >= l2.label_ts - INTERVAL {days} DAY
    GROUP BY l2.user_id, l2.label_ts
) f
  ON f.user_id = l.user_id AND f.label_ts = l.label_ts
