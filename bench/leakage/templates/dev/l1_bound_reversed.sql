-- leak: L1
-- param agg: SUM | COUNT
-- param cond: l.label_ts - e.ingested_at < INTERVAL 0 DAY | date_diff('second', l.label_ts, e.ingested_at) > 0 | epoch(e.ingested_at) > epoch(l.label_ts) | epoch(e.ingested_at) - epoch(l.label_ts) >= 1
SELECT l.user_id, l.label_ts, l.churned, {agg}(e.amount) AS amount_next_14d
FROM labels l
LEFT JOIN events e ON e.user_id = l.user_id
  AND {cond}
  AND e.ingested_at < l.label_ts + INTERVAL 14 DAY
GROUP BY l.user_id, l.label_ts, l.churned
