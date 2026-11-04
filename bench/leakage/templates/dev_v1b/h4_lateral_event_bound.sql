-- leak: L4
-- param days: 7 | 30
-- param bound: ev.event_ts < sp.label_ts | ev.event_ts <= sp.label_ts - INTERVAL '1 second'
-- Leaky: the lateral subquery bounds only by event time, so late-arriving events that had not been ingested yet at the prediction time are counted.
SELECT sp.user_id, sp.label_ts, sp.churned, r.n_recent, r.spend_recent
FROM labels AS sp
LEFT JOIN LATERAL (
  SELECT COUNT(*) AS n_recent, SUM(ev.amount) AS spend_recent
  FROM events AS ev
  WHERE ev.user_id = sp.user_id
    AND {bound}
    AND ev.event_ts >= sp.label_ts - INTERVAL '{days} days'
) AS r ON TRUE
