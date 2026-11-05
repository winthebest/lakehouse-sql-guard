-- leak: L3
-- param grain: day | week
-- COUNT(DISTINCT ...) of active periods in a correlated subquery with no time bound.
SELECT l.user_id, l.label_ts, l.churned,
       (SELECT COUNT(DISTINCT date_trunc('{grain}', e.event_ts))
        FROM events e
        WHERE e.user_id = l.user_id) AS n_active_{grain}s
FROM labels l
