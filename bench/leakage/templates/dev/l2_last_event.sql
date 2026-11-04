-- leak: L2
-- param field: e.amount | e.event_id
SELECT l.user_id, l.label_ts, l.churned,
       (SELECT {field} FROM events e WHERE e.user_id = l.user_id
        ORDER BY e.event_ts DESC LIMIT 1) AS last_value
FROM labels l
