-- leak: clean
-- param short: 3 | 7
-- param long: 28 | 56
-- Clean: both windows are carved out of one ingestion-bounded join, so nothing newer than the prediction time is aggregated.
SELECT lab.user_id, lab.label_ts, lab.churned,
       SUM(ev.amount) FILTER (WHERE ev.event_ts >= lab.label_ts - INTERVAL '{short} days') AS spend_short,
       SUM(ev.amount) AS spend_long
FROM labels AS lab
LEFT JOIN events AS ev ON ev.user_id = lab.user_id
  AND ev.ingested_at < lab.label_ts
  AND ev.event_ts >= lab.label_ts - INTERVAL '{long} days'
GROUP BY lab.user_id, lab.label_ts, lab.churned
