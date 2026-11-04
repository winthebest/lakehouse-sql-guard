-- leak: L7
-- param clock: current_date | now() | current_timestamp | today()
SELECT l.user_id, l.label_ts, l.churned,
       date_diff('day', MAX(e.event_ts), {clock}) AS days_since_last_event
FROM labels l
LEFT JOIN events e ON e.user_id = l.user_id AND e.ingested_at < l.label_ts
GROUP BY l.user_id, l.label_ts, l.churned
