-- leak: L4
-- param join: ASOF LEFT JOIN | ASOF JOIN
-- ASOF on event_ts < t picks the newest purchase by event time, which may have been loaded
-- only after t (late arrival).
SELECT l.user_id, l.label_ts, l.churned,
       e.amount AS last_purchase_amount,
       date_diff('hour', e.event_ts, l.label_ts) AS hours_since_last_purchase
FROM labels l
{join} events e
  ON e.user_id = l.user_id
 AND l.label_ts > e.event_ts
