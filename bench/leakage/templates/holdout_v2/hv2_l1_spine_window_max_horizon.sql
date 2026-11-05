-- leak: L1
-- param part: PARTITION BY en.country | PARTITION BY hour(l.label_ts) = 0
-- param unit: hour | day
-- MAX(label_ts) over a spine window with no ORDER BY spans the whole partition, including
-- spine rows with a later label_ts.
SELECT l.user_id, l.label_ts, l.churned,
       date_diff('{unit}', l.label_ts, MAX(l.label_ts) OVER ({part})) AS time_to_window_end
FROM labels l
JOIN entities en ON en.user_id = l.user_id
