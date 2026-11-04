-- leak: L1
-- param op: > | >=
-- Leaky: the time to the next event is measured from events that occur after the prediction time.
SELECT sp.user_id, sp.label_ts, sp.churned,
       MIN(ev.event_ts) AS next_event_ts,
       date_diff('hour', sp.label_ts, MIN(ev.event_ts)) AS hours_to_next
FROM labels AS sp
LEFT JOIN events AS ev ON ev.user_id = sp.user_id AND ev.event_ts {op} sp.label_ts
GROUP BY sp.user_id, sp.label_ts, sp.churned
