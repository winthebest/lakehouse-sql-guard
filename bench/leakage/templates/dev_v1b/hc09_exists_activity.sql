-- leak: clean
-- param hours: 24 | 72 | 336
-- Clean: the activity flag only considers events already ingested before the prediction time.
SELECT lab.user_id, lab.label_ts, lab.churned,
       CASE WHEN EXISTS (
         SELECT 1 FROM events AS ev
         WHERE ev.user_id = lab.user_id
           AND ev.ingested_at < lab.label_ts
           AND ev.event_ts >= lab.label_ts - INTERVAL {hours} HOUR
       ) THEN 1 ELSE 0 END AS active_{hours}h
FROM labels AS lab
