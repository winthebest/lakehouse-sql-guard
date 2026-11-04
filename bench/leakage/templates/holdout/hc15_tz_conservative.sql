-- leak: clean
-- param shift: 3 | 5 | 8
-- Clean: the local cutoff is moved earlier than the UTC prediction time, so only rows that had landed even earlier are read.
WITH spine AS (
  SELECT user_id, label_ts, churned, label_ts - INTERVAL '{shift} hours' AS local_cutoff
  FROM labels
)
SELECT sp.user_id, sp.label_ts, sp.churned, SUM(ev.amount) AS amt_local, COUNT(ev.event_id) AS n_local
FROM spine AS sp
LEFT JOIN events AS ev ON ev.user_id = sp.user_id
  AND ev.ingested_at < sp.local_cutoff
  AND ev.event_ts >= sp.local_cutoff - INTERVAL '30 days'
GROUP BY sp.user_id, sp.label_ts, sp.churned
