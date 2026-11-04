-- leak: clean
-- param lag: 10 | 14 | 21
-- Clean: only events older than the longest observed ingestion delay (8 days in this feed) are used, so each had landed by the prediction time even without an ingestion filter.
SELECT lab.user_id, lab.label_ts, lab.churned, SUM(ev.amount) AS amt_lagged
FROM labels AS lab
LEFT JOIN events AS ev ON ev.user_id = lab.user_id
  AND ev.event_ts < lab.label_ts - INTERVAL '{lag} days'
  AND ev.event_ts >= lab.label_ts - INTERVAL '90 days'
GROUP BY lab.user_id, lab.label_ts, lab.churned
