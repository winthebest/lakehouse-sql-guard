-- leak: clean
-- param agg: SUM | MAX
-- param days: 14 | 45
-- Clean: every CTE in the chain filters events to those ingested before the spine timestamp; the wrapper CTEs only reshape.
WITH base AS (
  SELECT user_id, label_ts, churned FROM labels
),
visible AS (
  SELECT b.user_id, b.label_ts, ev.amount
  FROM base AS b
  JOIN events AS ev ON ev.user_id = b.user_id
  WHERE ev.ingested_at < b.label_ts
    AND ev.event_ts >= b.label_ts - INTERVAL '{days} days'
),
rolled AS (
  SELECT user_id, label_ts, {agg}(amount) AS amt_{days}d, COUNT(*) AS n_{days}d
  FROM visible
  GROUP BY user_id, label_ts
)
SELECT b.user_id, b.label_ts, b.churned, r.amt_{days}d, r.n_{days}d
FROM base AS b
LEFT JOIN rolled AS r USING (user_id, label_ts)
