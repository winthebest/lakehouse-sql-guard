-- leak: clean
-- param lb: TRUE | ev.event_ts >= lab.label_ts - INTERVAL '30 days' | ev.event_ts > date_add(lab.label_ts, -INTERVAL '90 days')
-- Clean: all history is allowed here because the ingestion bound limits it to rows that had landed before the prediction time.
SELECT lab.user_id, lab.label_ts, lab.churned,
       MAX(ev.event_ts) AS last_event_ts, MAX(ev.ingested_at) AS last_ingested_at, COUNT(ev.event_id) AS n_events
FROM labels AS lab
LEFT JOIN events AS ev ON ev.user_id = lab.user_id AND ev.ingested_at < lab.label_ts AND {lb}
GROUP BY ALL
