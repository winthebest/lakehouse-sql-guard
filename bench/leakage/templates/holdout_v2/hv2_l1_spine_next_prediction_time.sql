-- leak: L1
-- param unit: hour | day
-- The next prediction time in the spine (any user) is a later spine row: it does not exist
-- yet at t.
SELECT l.user_id, l.label_ts, l.churned,
       date_diff('{unit}', l.label_ts,
                 (SELECT MIN(n.label_ts) FROM labels n WHERE n.label_ts > l.label_ts))
           AS time_to_next_prediction
FROM labels l
