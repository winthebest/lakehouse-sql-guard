-- leak: L4
-- param hours: 24 | 72
-- Leaky: the activity flag looks at events by event time only, but events of the last day or two had not been ingested at the prediction time.
SELECT lab.user_id, lab.label_ts, lab.churned,
       CASE WHEN EXISTS (
         SELECT 1 FROM events AS ev
         WHERE ev.user_id = lab.user_id
           AND ev.event_ts BETWEEN lab.label_ts - INTERVAL {hours} HOUR AND lab.label_ts
       ) THEN 1 ELSE 0 END AS active_{hours}h
FROM labels AS lab
