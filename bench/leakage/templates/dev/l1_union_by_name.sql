-- leak: L1
-- param agg: SUM | COUNT
WITH ev AS (
  SELECT user_id, ingested_at AS t, amount FROM events
  UNION ALL BY NAME
  SELECT user_id, ingested_at AS x, amount, ingested_at - INTERVAL 30 DAY AS t FROM events
)
SELECT l.user_id, l.label_ts, l.churned, {agg}(ev.amount) AS amount_before
FROM labels l
LEFT JOIN ev ON ev.user_id = l.user_id AND ev.t < l.label_ts
GROUP BY l.user_id, l.label_ts, l.churned
