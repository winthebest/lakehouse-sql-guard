-- leak: L5
-- param tz: 7 | 9
-- Leaky: the availability bound is shifted forward by the zone offset, admitting rows ingested after the true prediction instant.
SELECT lab.user_id, lab.label_ts, lab.churned, MAX(ev.amount) AS max_amt
FROM labels AS lab
LEFT JOIN events AS ev ON ev.user_id = lab.user_id
  AND ev.ingested_at < date_add(lab.label_ts, INTERVAL {tz} HOUR)
  AND ev.event_ts >= lab.label_ts - INTERVAL '30 days'
GROUP BY lab.user_id, lab.label_ts, lab.churned
