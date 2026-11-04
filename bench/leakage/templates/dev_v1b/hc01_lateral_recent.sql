-- leak: clean
-- param days: 7 | 45
-- param avail: ev.ingested_at < sp.label_ts | ev.ingested_at <= sp.label_ts - INTERVAL '1 second' | ev.ingested_at < sp.label_ts - INTERVAL '6 hours'
-- Clean: the lateral subquery only reads rows already ingested before the prediction time, within a bounded event-time window.
SELECT sp.user_id, sp.label_ts, sp.churned, r.n_recent, r.spend_recent
FROM labels AS sp
LEFT JOIN LATERAL (
  SELECT COUNT(*) AS n_recent, SUM(ev.amount) AS spend_recent
  FROM events AS ev
  WHERE ev.user_id = sp.user_id
    AND {avail}
    AND ev.event_ts >= sp.label_ts - INTERVAL '{days} days'
) AS r ON TRUE
