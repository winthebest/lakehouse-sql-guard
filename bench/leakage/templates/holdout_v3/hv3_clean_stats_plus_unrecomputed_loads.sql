-- leak: clean
-- Nowcast of lifetime spend: last stats row before t plus purchases loaded since that recompute.
WITH last_stats AS (
    SELECT l.user_id, l.label_ts, l.churned, s.updated_at AS stats_ts, s.total_spend
    FROM labels l
    ASOF LEFT JOIN user_stats s
      ON s.user_id = l.user_id
     AND l.label_ts > s.updated_at
)
SELECT ls.user_id, ls.label_ts, ls.churned,
       ls.total_spend,
       COALESCE(SUM(e.amount), 0)                  AS spend_loaded_since_stats,
       ls.total_spend + COALESCE(SUM(e.amount), 0) AS spend_nowcast
FROM last_stats ls
LEFT JOIN events e
       ON e.user_id = ls.user_id
      AND e.ingested_at >= ls.stats_ts
      AND e.ingested_at <  ls.label_ts
GROUP BY ls.user_id, ls.label_ts, ls.churned, ls.total_spend
