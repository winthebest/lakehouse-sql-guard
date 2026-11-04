-- leak: clean
-- param window: 7 | 14 | 30 | 90
-- param join: JOIN | LEFT JOIN
WITH ev AS (
  SELECT l.user_id, l.label_ts, COUNT(e.event_id) AS n_recent
  FROM labels l
  LEFT JOIN events e ON e.user_id = l.user_id AND e.ingested_at < l.label_ts
    AND e.event_ts >= l.label_ts - INTERVAL {window} DAY
  GROUP BY l.user_id, l.label_ts
)
SELECT l.user_id, l.label_ts, l.churned, s.total_spend, ev.n_recent AS n_{window}d, u.country
FROM labels l
ASOF LEFT JOIN user_stats s ON l.user_id = s.user_id AND l.label_ts > s.updated_at
{join} ev ON ev.user_id = l.user_id AND ev.label_ts = l.label_ts
{join} entities u ON u.user_id = l.user_id
