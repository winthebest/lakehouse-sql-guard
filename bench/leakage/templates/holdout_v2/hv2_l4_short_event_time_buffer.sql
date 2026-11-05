-- leak: L4
-- param buffer: 1 | 3 | 7
-- param span: 30 | 60
-- A safety buffer on event time that is shorter than the 8-day maximum load delay: a
-- purchase from t - buffer - 1h can still be loaded at or after t.
SELECT l.user_id, l.label_ts, l.churned,
       (SELECT COALESCE(SUM(e.amount), 0)
        FROM events e
        WHERE e.user_id = l.user_id
          AND e.event_ts <  l.label_ts - INTERVAL {buffer} DAY
          AND e.event_ts >= l.label_ts - INTERVAL {span} DAY) AS spend_buffered
FROM labels l
