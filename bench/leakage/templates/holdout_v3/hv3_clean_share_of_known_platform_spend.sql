-- leak: clean
-- param w: 7 | 30
-- The normaliser is the platform spend already loaded at t, not over all history.
SELECT l.user_id, l.label_ts, l.churned,
       SUM(e.amount) FILTER (WHERE e.user_id = l.user_id) / NULLIF(SUM(e.amount), 0) AS share_of_platform_spend,
       COUNT(DISTINCT e.user_id)                                                    AS active_buyers_known
FROM labels l
LEFT JOIN events e
       ON e.ingested_at < l.label_ts
      AND e.event_ts >= l.label_ts - INTERVAL {w} DAY
GROUP BY l.user_id, l.label_ts, l.churned
