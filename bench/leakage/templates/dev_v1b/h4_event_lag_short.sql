-- leak: L4
-- param lag: 6 | 36 | 72
-- Leaky: a short event-time lag does not cover the ingestion delay of late events (up to 8 days), so some included events had not landed yet.
SELECT lab.user_id, lab.label_ts, lab.churned, SUM(ev.amount) AS amt_lagged
FROM labels AS lab
LEFT JOIN events AS ev ON ev.user_id = lab.user_id
  AND ev.event_ts < lab.label_ts - INTERVAL '{lag} hours'
  AND ev.event_ts >= lab.label_ts - INTERVAL '90 days'
GROUP BY lab.user_id, lab.label_ts, lab.churned
