-- leak: L5
-- param tz: 7 | 9
-- Leaky: converting the prediction time to a zone ahead of UTC pushes the ingestion cutoff forward past rows that landed after the true prediction instant.
WITH spine AS (
  SELECT user_id, label_ts, churned, label_ts + INTERVAL '{tz} hours' AS local_cutoff
  FROM labels
)
SELECT sp.user_id, sp.label_ts, sp.churned, SUM(ev.amount) AS amt_local, COUNT(ev.event_id) AS n_local
FROM spine AS sp
LEFT JOIN events AS ev ON ev.user_id = sp.user_id
  AND ev.ingested_at < sp.local_cutoff
  AND ev.event_ts >= sp.local_cutoff - INTERVAL '30 days'
GROUP BY sp.user_id, sp.label_ts, sp.churned
