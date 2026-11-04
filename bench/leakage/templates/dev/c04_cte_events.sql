-- leak: clean
-- param agg: SUM | AVG | MAX | MIN
-- param window: 7 | 14 | 30 | 90
-- param join: JOIN | LEFT JOIN
WITH feats AS (
  SELECT l.user_id, l.label_ts, COUNT(*) AS n_events, {agg}(e.amount) AS amt
  FROM labels l
  JOIN events e ON e.user_id = l.user_id
  WHERE e.ingested_at < l.label_ts AND e.event_ts >= l.label_ts - INTERVAL {window} DAY
  GROUP BY l.user_id, l.label_ts
)
SELECT l.user_id, l.label_ts, l.churned, f.n_events AS n_events_{window}d, f.amt
FROM labels l
{join} feats f ON f.user_id = l.user_id AND f.label_ts = l.label_ts
